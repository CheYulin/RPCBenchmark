/**
 * Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include "datasystem/common/rpc/coro_unary.h"

#include <algorithm>
#include <any>
#include <array>
#include <atomic>
#include <bit>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdlib>
#include <limits>
#include <mutex>
#include <new>
#include <string_view>
#include <system_error>
#include <thread>
#include <utility>
#include <vector>

#include <asio/bind_allocator.hpp>
#include <bthread/condition_variable.h>
#include <ylt/coro_rpc/coro_rpc_client.hpp>
#include <ylt/coro_rpc/coro_rpc_server.hpp>

#include "datasystem/common/rpc/bthread_utils.h"
#include "datasystem/common/rpc/coro_caller_io.h"

#ifndef DS_PRIVATE_CORO_REQUEST_MARKERS
#define DS_PRIVATE_CORO_REQUEST_MARKERS 0
#endif
#if DS_PRIVATE_CORO_REQUEST_MARKERS != 0 && DS_PRIVATE_CORO_REQUEST_MARKERS != 1
#error "DS_PRIVATE_CORO_REQUEST_MARKERS must be 0 or 1"
#endif
#if DS_PRIVATE_CORO_REQUEST_MARKERS == 1
#if !defined(__GNUC__) || defined(__clang__) || __GNUC__ < 15
#error "Private request markers require GNU15 or newer"
#endif
#include "private_request_marker.h"
extern "C" __attribute__((noipa, used, visibility("default")))
void DSPrivateCoroServerRequestPhase(std::uint32_t requestId, std::uint32_t phase) noexcept
{
    asm volatile("" : : "r"(requestId), "r"(phase) : "memory");
}
#endif

namespace datasystem::coro_unary {
namespace {
using Clock = std::chrono::steady_clock;
using std::chrono::milliseconds;
using std::chrono::microseconds;
using async_simple::coro::Lazy;

constexpr size_t K_IO_THREADS = 4;
constexpr size_t K_CLEANUP_BATCH = 32;
constexpr size_t K_CLOSE_STORAGE_BYTES = 1024;
constexpr auto K_CLEANUP_WAIT = std::chrono::milliseconds(1);
constexpr size_t K_LANES = std::numeric_limits<uint64_t>::digits;
constexpr size_t K_MAX_CONNECTIONS = 128;
constexpr size_t K_MAX_JOBS = 64;
constexpr size_t K_MAX_PENDING_PER_CONNECTION = 2;
constexpr size_t K_MAX_FRAME_BYTES = kMaxRequestBytes + 4 * 1024;
constexpr size_t K_MAX_REPLY_MESSAGE_BYTES = 2 * 1024;
constexpr size_t K_MAX_METHOD_BYTES = 512;
constexpr size_t K_MAX_SERVER_ATTACHMENT_BYTES = 128 * 1024 * 1024;
constexpr uint32_t K_INVOKE_V2_METHOD_ID = 0x44535232;
constexpr auto K_IDLE_TIMEOUT = std::chrono::seconds(5);
constexpr auto K_REUSE_IDLE_US =
    std::chrono::duration_cast<microseconds>(K_IDLE_TIMEOUT - std::chrono::seconds(1)).count();
constexpr char K_LOOPBACK[] = "127.0.0.1";

std::atomic<size_t> g_serverAttachmentBytes{ 0 };

#if DS_PRIVATE_CORO_REQUEST_MARKERS == 1
std::atomic<std::uint64_t> g_privateServerRequestId{ 0 };
#endif

struct CloseStorage {
    ~CloseStorage() = default;
    alignas(std::max_align_t) std::array<std::byte, K_CLOSE_STORAGE_BYTES> bytes{};
    bool allocated = false;
};

template <typename T>
class CloseAllocator {
public:
    using value_type = T;

    explicit CloseAllocator(CloseStorage &storage) : storage_(&storage)
    {
    }

    template <typename U>
    CloseAllocator(const CloseAllocator<U> &other) : storage_(other.Storage())
    {
    }

    ~CloseAllocator() = default;

    T *allocate(size_t count)
    {
        static_assert(sizeof(T) <= K_CLOSE_STORAGE_BYTES && alignof(T) <= alignof(std::max_align_t));
        if (count != 1 || storage_->allocated) {
            throw std::bad_alloc();
        }
        storage_->allocated = true;
        return reinterpret_cast<T *>(storage_->bytes.data());
    }

    void deallocate(T *, size_t) noexcept
    {
        storage_->allocated = false;
    }

    CloseStorage *Storage() const
    {
        return storage_;
    }

    template <typename U>
    bool operator==(const CloseAllocator<U> &other) const
    {
        return storage_ == other.Storage();
    }

private:
    CloseStorage *storage_;
};

class AttachmentCharge {
public:
    explicit AttachmentCharge(size_t bytes)
    {
        auto used = g_serverAttachmentBytes.load(std::memory_order_relaxed);
        while (bytes <= K_MAX_SERVER_ATTACHMENT_BYTES - used) {
            if (g_serverAttachmentBytes.compare_exchange_weak(used, used + bytes, std::memory_order_relaxed)) {
                bytes_ = bytes;
                return;
            }
        }
    }

    ~AttachmentCharge()
    {
        if (bytes_ != 0) {
            g_serverAttachmentBytes.fetch_sub(bytes_, std::memory_order_relaxed);
        }
    }

    AttachmentCharge(const AttachmentCharge &) = delete;
    AttachmentCharge &operator=(const AttachmentCharge &) = delete;

    bool Reserved() const
    {
        return bytes_ != 0;
    }

private:
    size_t bytes_ = 0;
};

struct ReplyAttachment {
    explicit ReplyAttachment(std::string data) : bytes(std::move(data)), charge(bytes.size())
    {
    }

    std::string bytes;
    AttachmentCharge charge;
};

struct WireRequest {
    uint16_t workerPort;
    uint64_t remainingUs;
    uint8_t logSampleState;
    std::string method;
    std::string traceId;
    std::string protobuf;
};

struct WireReply {
    uint32_t code;
    std::string message;
    std::string protobuf;
};

using YltProtocol = coro_rpc::protocol::coro_rpc_protocol;
struct BoundedProtocol : YltProtocol {
    using router = coro_rpc::protocol::router<BoundedProtocol>;

    template <typename Socket>
    static Lazy<std::error_code> read_payload(Socket &socket, req_header &header, std::string &body,
                                             coro_io::heterogeneous_buffer &attachment,
                                             coro_rpc::context_info_t<BoundedProtocol> *context)
    {
        if (header.length > K_MAX_FRAME_BYTES || header.attach_length > kMaxAttachmentBytes) {
            co_return std::make_error_code(std::errc::message_size);
        }
        auto &tag = context->tag();
        auto *pending = std::any_cast<size_t>(&tag);
        if (pending == nullptr) {
            pending = &tag.emplace<size_t>(0);
        }
        // The next request can arrive before Asio reports the previous write complete.
        if (*pending >= K_MAX_PENDING_PER_CONNECTION) {
            co_return std::make_error_code(std::errc::no_buffer_space);
        }
        try {
            if (header.attach_length == 0) {
                context->set_complete_handler([pending](const std::error_code &, size_t) { --*pending; });
            } else {
                auto charge = std::make_shared<AttachmentCharge>(header.attach_length);
                if (!charge->Reserved()) {
                    co_return std::make_error_code(std::errc::no_buffer_space);
                }
                context->set_complete_handler(
                    [pending, charge = std::move(charge)](const std::error_code &, size_t) mutable {
                        --*pending;
                        charge.reset();
                    });
            }
        } catch (const std::bad_alloc &) {
            co_return std::make_error_code(std::errc::not_enough_memory);
        }
        ++*pending;
        co_return co_await YltProtocol::read_payload(socket, header, body, attachment);
    }
};

struct BoundedConfig : coro_rpc::config_t {
    // YLT's server_config contract requires this alias name.
    using rpc_protocol = BoundedProtocol;
};
using YltServer = coro_rpc::coro_rpc_server_base<BoundedConfig>;
using YltClient = coro_rpc::coro_rpc_client;
using ReplyContext = coro_rpc::context_base<WireReply, BoundedProtocol>;

StatusCode CheckRequest(const Request &request)
{
    const int64_t now = NowUs();
    if (request.deadlineUs <= now) {
        return K_RPC_DEADLINE_EXCEEDED;
    }
    if (request.deadlineUs - now > kMaxBudgetUs || request.workerPort == 0 || request.traceId.size() > kMaxTraceBytes
        || request.protobuf.size() > kMaxRequestBytes || request.method.size() > K_MAX_METHOD_BYTES
        || request.attachment.size() > kMaxAttachmentBytes) {
        return K_INVALID;
    }
    return K_OK;
}

Reply Failure(StatusCode code)
{
    return { code, {}, {} };
}

Status FailureStatus(StatusCode code)
{
    switch (code) {
        case K_RPC_DEADLINE_EXCEEDED:
            return { code, "coro_rpc deadline exceeded" };
        case K_INVALID:
            return { code, "Invalid coro_rpc request" };
        case K_OUT_OF_MEMORY:
            return { code, "coro_rpc ran out of memory" };
        case K_TRY_AGAIN:
            return { code, "coro_rpc client lanes are busy" };
        default:
            return { code, "coro_rpc transport failed" };
    }
}

class Dispatcher {
public:
    explicit Dispatcher(Handler handler) : handler_(std::move(handler))
    {
    }

    ~Dispatcher() = default;

    void Open()
    {
        std::lock_guard lock(mutex_);
        accepting_ = true;
    }

    void CloseAndWait()
    {
        std::unique_lock lock(mutex_);
        accepting_ = false;
        drained_.wait(lock, [this] { return jobs_ == 0; });
    }

    void InvokeV2(ReplyContext context, WireRequest wire)
    {
        if (wire.remainingUs == 0 || wire.remainingUs > static_cast<uint64_t>(kMaxBudgetUs)) {
            Respond(std::move(context), Failure(wire.remainingUs == 0 ? K_RPC_DEADLINE_EXCEEDED : K_INVALID));
            return;
        }
        Request request{ wire.workerPort, NowUs() + static_cast<int64_t>(wire.remainingUs), std::move(wire.traceId),
                         std::move(wire.protobuf), wire.logSampleState, std::move(wire.method), {} };
        auto code = CheckRequest(request);
        if (code != K_OK) {
            Respond(std::move(context), Failure(code));
            return;
        }

        code = Admit();
        if (code != K_OK) {
            Respond(std::move(context), Failure(code));
            return;
        }

        try {
            auto *info = context.get_context_info();
            if (!info->get_request_attachment().empty()) {
                request.attachment = info->release_request_attachment();
            }
#if DS_PRIVATE_CORO_REQUEST_MARKERS == 1
            const auto privateRequestId = private_diagnostic::NextRequestId(
                g_privateServerRequestId, private_diagnostic::kRequestIdCeiling);
            DSPrivateCoroServerRequestPhase(privateRequestId, private_diagnostic::kSubmission);
#endif
            if (StartBackgroundTask(nullptr, [this, context, request = std::move(request)
#if DS_PRIVATE_CORO_REQUEST_MARKERS == 1
                , privateRequestId
#endif
            ]() mutable {
#if DS_PRIVATE_CORO_REQUEST_MARKERS == 1
                DSPrivateCoroServerRequestPhase(privateRequestId, private_diagnostic::kLambdaEntry);
                auto reply = Run(std::move(request));
                DSPrivateCoroServerRequestPhase(privateRequestId, private_diagnostic::kReplyReady);
                Respond(std::move(context), std::move(reply));
#else
                Respond(std::move(context), Run(std::move(request)));
#endif
                Finish();
            }) == 0) {
                return;
            }
            code = K_RPC_UNAVAILABLE;
        } catch (const std::bad_alloc &) {
            code = K_OUT_OF_MEMORY;
        }
        Finish();
        Respond(std::move(context), Failure(code));
    }

private:
    StatusCode Admit()
    {
        std::lock_guard lock(mutex_);
        if (!accepting_) {
            return K_SHUTTING_DOWN;
        }
        if (jobs_ == K_MAX_JOBS) {
            return K_TRY_AGAIN;
        }
        ++jobs_;
        return K_OK;
    }

    Reply Run(Request request)
    {
        try {
            if (request.deadlineUs <= NowUs()) {
                return Failure(K_RPC_DEADLINE_EXCEEDED);
            }
            auto reply = handler_(std::move(request));
            if (reply.protobuf.size() > kMaxRequestBytes || reply.message.size() > K_MAX_REPLY_MESSAGE_BYTES
                || reply.attachment.size() > kMaxAttachmentBytes) {
                return Failure(K_OUT_OF_RANGE);
            }
            return reply;
        } catch (const std::bad_alloc &) {
            return Failure(K_OUT_OF_MEMORY);
        } catch (...) {
            return Failure(K_RUNTIME_ERROR);
        }
    }

    static void Respond(ReplyContext context, Reply reply) noexcept
    {
        try {
            if (!reply.attachment.empty()) {
                auto attachment = std::make_shared<ReplyAttachment>(std::move(reply.attachment));
                if (attachment->charge.Reserved()) {
                    context.get_context_info()->set_response_attachment(
                        [attachment = std::move(attachment)] { return std::string_view(attachment->bytes); });
                } else {
                    reply = Failure(K_RUNTIME_ERROR);
                }
            }
            context.response_msg(WireReply{ reply.code, std::move(reply.message), std::move(reply.protobuf) });
        } catch (...) {
            try {
                context.get_context_info()->close();
            } catch (...) {
                // Scheduling the close can also fail under memory pressure. The peer deadline still applies.
            }
        }
    }

    void Finish()
    {
        std::lock_guard lock(mutex_);
        if (--jobs_ == 0 && !accepting_) {
            drained_.notify_one();
        }
    }

    const Handler handler_;
    std::mutex mutex_;
    std::condition_variable drained_;
    size_t jobs_ = 0;
    bool accepting_ = false;
};

struct CallState {
    bthread::Mutex mutex;
    bthread::ConditionVariable completed;
    StatusCode status = K_OK;
    Reply reply{};
    bool done = false;
};
}  // namespace
}  // namespace datasystem::coro_unary

namespace coro_rpc {
template <>
consteval auto func_id<&datasystem::coro_unary::Dispatcher::InvokeV2>()
{
    return datasystem::coro_unary::K_INVOKE_V2_METHOD_ID;
}
}  // namespace coro_rpc

namespace datasystem::coro_unary {

int64_t NowUs()
{
    return std::chrono::duration_cast<microseconds>(Clock::now().time_since_epoch()).count();
}

class Client::Impl {
public:
    Impl(std::string host, uint16_t port, int32_t connectTimeoutMs)
        : host_(std::move(host)), port_(port), connectTimeoutMs_(connectTimeoutMs), service_(std::to_string(port))
    {
        std::error_code error;
        address_ = asio::ip::make_address(host_, error);
        numericHost_ = !error;
        if (const auto *option = std::getenv("DATASYSTEM_CORO_CALLER_IO")) {
            const std::string_view value(option);
            callerIo_ = value == "1";
            callerIoValid_ = callerIo_ || value == "0";
        }
    }

    Status GetInitStatus() const
    {
        if (cleanupFailed_.load(std::memory_order_acquire)) {
            return { K_RPC_UNAVAILABLE, "coro_rpc caller IO cleanup failed; recreate the client." };
        }
        if (!callerIoValid_) {
            return { K_INVALID, "DATASYSTEM_CORO_CALLER_IO must be 0 or 1." };
        }
        if (!numericHost_ || port_ == 0) {
            return { K_INVALID, "coro_rpc requires an IP literal and a valid port; DNS resolution is unsupported." };
        }
        return Status::OK();
    }

    ~Impl()
    {
        WaitForLanes();
        CloseOwnedLanes();
        if (pool_ == nullptr) {
            return;
        }
        for (auto &lane : lanes_) {
            if (lane.client) {
                lane.client->close();
            }
        }
        // YLT's detached timer callbacks can refer to the client until IO has drained.
        pool_->stop();
        ioThread_.join();
        for (auto &lane : lanes_) {
            lane.client.reset();
        }
    }

    Status Call(Request request, Reply &reply)
    {
        auto init = GetInitStatus();
        if (init.IsError()) {
            return init;
        }
        auto code = CheckRequest(request);
        if (code != K_OK) {
            return FailureStatus(code);
        }
        const bool owned = callerIo_ && bthread_self() == INVALID_BTHREAD;
        try {
            if (owned) {
                std::call_once(ownedStartOnce_, [this] { cleanupThread_ = std::thread([this] { CleanupLoop(); }); });
            } else {
                std::call_once(startOnce_, [this] { StartIo(); });
            }
        } catch (const std::bad_alloc &) {
            return FailureStatus(K_OUT_OF_MEMORY);
        } catch (const std::system_error &) {
            return FailureStatus(K_RPC_UNAVAILABLE);
        }
        const auto laneId = AcquireLane();
        if (laneId == K_LANES) {
            return FailureStatus(K_TRY_AGAIN);
        }
        return owned ? CallOwned(laneId, std::move(request), reply) : CallPooled(laneId, std::move(request), reply);
    }

private:
    enum class RequestDeadlineOwner { Transport, CallerPump };

    struct Lane {
        coro_io::ExecutorWrapper<> *executor = nullptr;
        std::unique_ptr<YltClient> client;
        int64_t lastSendUs = 0;
    };

    enum class Retirement { Ready, Closing, FinalDrain };
    struct OwnedLane {
        OwnedLane() : executor(context.get_executor())
        {
            lane.executor = &executor;
        }
        ~OwnedLane() = default;
        OwnedLane(const OwnedLane &) = delete;
        OwnedLane &operator=(const OwnedLane &) = delete;
        asio::io_context context;
        coro_io::ExecutorWrapper<> executor;
        Lane lane;
        CloseStorage closeStorage;
        std::shared_ptr<CallState> state;
        std::atomic<bool> complete{ true };
        std::thread::id runner;
        Retirement retirement = Retirement::Ready;
    };

    Status CallPooled(size_t laneId, Request request, Reply &reply)
    {
        std::shared_ptr<CallState> state;
        const auto deadline = request.deadlineUs;
        try {
            state = std::make_shared<CallState>();
            auto &lane = lanes_[laneId];
            Invoke(lane, std::move(request), state, RequestDeadlineOwner::Transport).via(lane.executor).start(
                [this, laneId, state](async_simple::Try<StatusCode> &&result) {
                    {
                        std::lock_guard lock(state->mutex);
                        state->status = result.hasError() ? K_RUNTIME_ERROR : result.value();
                        state->done = true;
                    }
                    state->completed.notify_one();
                    ReleaseLane(laneId);
                });
        } catch (const std::bad_alloc &) {
            ReleaseLane(laneId);
            return FailureStatus(K_OUT_OF_MEMORY);
        } catch (const std::system_error &) {
            ReleaseLane(laneId);
            return FailureStatus(K_RPC_UNAVAILABLE);
        }
        return WaitReply(*state, deadline, reply);
    }

    static Status WaitReply(CallState &state, int64_t deadline, Reply &reply)
    {
        std::unique_lock lock(state.mutex);
        while (!state.done) {
            const auto remaining = deadline - NowUs();
            if (remaining <= 0) {
                return FailureStatus(K_RPC_DEADLINE_EXCEEDED);
            }
            state.completed.wait_for(lock, remaining);
        }
        return FinishReply(state, deadline, reply);
    }

    static Status FinishReply(CallState &state, int64_t deadline, Reply &reply)
    {
        if (deadline <= NowUs()) {
            return FailureStatus(K_RPC_DEADLINE_EXCEEDED);
        }
        if (state.status != K_OK) {
            return FailureStatus(state.status);
        }
        reply = std::move(state.reply);
        return Status::OK();
    }

    Status CallOwned(size_t laneId, Request request, Reply &reply)
    {
        try {
            if (ownedLanes_[laneId] == nullptr) {
                auto owned = std::make_unique<OwnedLane>();
                owned->lane.client = CreateClient(owned->lane.executor, RequestDeadlineOwner::CallerPump);
                ownedLanes_[laneId] = std::move(owned);
            }
            ownedLanes_[laneId]->state = std::make_shared<CallState>();
        } catch (const std::bad_alloc &) {
            ReleaseLane(laneId);
            return FailureStatus(K_OUT_OF_MEMORY);
        } catch (const std::system_error &) {
            ReleaseLane(laneId);
            return FailureStatus(K_RPC_UNAVAILABLE);
        }
        auto &owned = *ownedLanes_[laneId];
        const auto deadline = request.deadlineUs;
        owned.runner = std::this_thread::get_id();
        owned.complete.store(false, std::memory_order_relaxed);
        if (owned.context.stopped()) {
            owned.context.restart();
        }
        StatusCode failure = K_OK;
        try {
            StartOwned(owned, std::move(request));
            const auto end = Clock::time_point(microseconds(deadline));
            const auto result = detail::PumpCallerUntil(owned.context, end, [&owned] {
                return owned.complete.load(std::memory_order_acquire);
            });
            if (result != detail::CallerPumpResult::Complete || deadline <= NowUs()) {
                failure = result == detail::CallerPumpResult::NoWork ? K_RUNTIME_ERROR : K_RPC_DEADLINE_EXCEEDED;
            }
        } catch (const std::bad_alloc &) {
            failure = K_OUT_OF_MEMORY;
        } catch (...) {
            failure = K_RUNTIME_ERROR;
        }
        if (failure != K_OK) {
            Retire(laneId);
            return FailureStatus(failure);
        }
        const auto code = deadline <= NowUs() ? K_RPC_DEADLINE_EXCEEDED : owned.state->status;
        if (code != K_OK) {
            Retire(laneId);
            return FailureStatus(code);
        }
        reply = std::move(owned.state->reply);
        owned.state.reset();
        ReleaseLane(laneId);
        return Status::OK();
    }

    void StartOwned(OwnedLane &owned, Request request)
    {
        try {
            Invoke(owned.lane, std::move(request), owned.state, RequestDeadlineOwner::CallerPump)
                .via(&owned.executor).start(
                [&owned](async_simple::Try<StatusCode> &&result) {
                    owned.state->status = result.hasError() || !owned.executor.currentThreadInExecutor()
                                                  || owned.runner != std::this_thread::get_id()
                                              ? K_RUNTIME_ERROR
                                              : result.value();
                    owned.complete.store(true, std::memory_order_release);
                });
        } catch (...) {
            owned.complete.store(true, std::memory_order_release);
            throw;
        }
    }

    void Retire(size_t laneId)
    {
        {
            std::lock_guard lock(cleanupMutex_);
            retiring_ |= uint64_t{ 1 } << laneId;
        }
        cleanupReady_.notify_one();
    }

    bool PollRetirement(OwnedLane &owned)
    {
        owned.runner = std::this_thread::get_id();
        if (owned.retirement == Retirement::Ready) {
            // Close must dispatch inline: its YLT closed flag is committed before dispatch can allocate.
            asio::post(owned.context, asio::bind_allocator(CloseAllocator<std::byte>(owned.closeStorage), [&owned] {
                if (owned.lane.client) {
                    owned.lane.client->close();
                }
            }));
            owned.retirement = Retirement::Closing;
        }
        if (owned.context.stopped()) {
            owned.context.restart();
        }
        for (size_t i = 0; i < K_CLEANUP_BATCH && owned.context.poll_one() != 0; ++i) {
        }
        if (!detail::CallerContextDrained(owned.context, owned.complete.load(std::memory_order_acquire))) {
            return false;
        }
        if (owned.retirement == Retirement::Closing) {
            owned.lane.client.reset();
            owned.lane.lastSendUs = 0;
            owned.context.restart();
            owned.retirement = Retirement::FinalDrain;
            return false;
        }
        owned.state.reset();
        owned.retirement = Retirement::Ready;
        return true;
    }

    void CleanupLoop()
    {
        std::unique_lock lock(cleanupMutex_);
        for (;;) {
            cleanupReady_.wait(lock, [this] { return cleanupStopping_ || retiring_ != 0; });
            if (cleanupStopping_ && retiring_ == 0) {
                return;
            }
            const auto pending = retiring_;
            lock.unlock();
            for (size_t laneId = 0; laneId < K_LANES; ++laneId) {
                const auto bit = uint64_t{ 1 } << laneId;
                bool drained = false;
                if ((pending & bit) != 0) {
                    try {
                        drained = PollRetirement(*ownedLanes_[laneId]);
                    } catch (...) {
                        cleanupFailed_.store(true, std::memory_order_release);
                    }
                }
                if (drained) {
                    {
                        std::lock_guard queueLock(cleanupMutex_);
                        retiring_ &= ~bit;
                    }
                    ReleaseLane(laneId);
                }
            }
            lock.lock();
            if (retiring_ != 0) {
                cleanupReady_.wait_for(lock, K_CLEANUP_WAIT);
            }
        }
    }

    void WaitForLanes()
    {
        for (auto active = busy_.load(); active != 0; active = busy_.load()) {
            busy_.wait(active);
        }
    }

    void CloseOwnedLanes()
    {
        if (!cleanupThread_.joinable()) {
            return;
        }
        for (size_t laneId = 0; laneId < K_LANES; ++laneId) {
            if (ownedLanes_[laneId]) {
                busy_.fetch_or(uint64_t{ 1 } << laneId, std::memory_order_relaxed);
                Retire(laneId);
            }
        }
        WaitForLanes();
        {
            std::lock_guard lock(cleanupMutex_);
            cleanupStopping_ = true;
        }
        cleanupReady_.notify_one();
        cleanupThread_.join();
    }

    void StartIo()
    {
        auto pool = std::make_unique<coro_io::io_context_pool>(K_IO_THREADS);
        auto thread = std::thread([runtime = pool.get()] { runtime->run(); });
        for (auto &lane : lanes_) {
            lane.executor = pool->get_executor();
        }
        pool_ = std::move(pool);
        ioThread_ = std::move(thread);
    }

    size_t AcquireLane()
    {
        auto busy = busy_.load(std::memory_order_relaxed);
        while (~busy != 0) {
            const auto lane = std::countr_zero(~busy);
            const auto next = busy | (uint64_t{ 1 } << lane);
            if (busy_.compare_exchange_weak(busy, next, std::memory_order_acquire, std::memory_order_relaxed)) {
                return lane;
            }
        }
        return K_LANES;
    }

    void ReleaseLane(size_t lane)
    {
        const auto bit = uint64_t{ 1 } << lane;
        if (busy_.fetch_and(~bit, std::memory_order_release) == bit) {
            busy_.notify_all();
        }
    }

    Lazy<StatusCode> Invoke(Lane &lane, Request request, std::shared_ptr<CallState> state,
                          RequestDeadlineOwner deadlineOwner)
    {
        try {
            co_return co_await Exchange(lane, request, *state, deadlineOwner);
        } catch (const std::bad_alloc &) {
            if (lane.client) {
                lane.client->close();
            }
            co_return K_OUT_OF_MEMORY;
        } catch (...) {
            if (lane.client) {
                lane.client->close();
            }
            co_return K_RUNTIME_ERROR;
        }
    }

    Lazy<StatusCode> Exchange(Lane &lane, Request &request, CallState &state, RequestDeadlineOwner deadlineOwner)
    {
        const auto nowUs = NowUs();
        if (request.deadlineUs <= nowUs) {
            co_return K_RPC_DEADLINE_EXCEEDED;
        }
        // YLT stops reading when no request is outstanding, so an idle server FIN can go unnoticed.
        const bool needsConnect = lane.lastSendUs == 0 || lane.client == nullptr || lane.client->has_closed()
                                  || nowUs - lane.lastSendUs >= K_REUSE_IDLE_US;
        if (lane.client == nullptr) {
            try {
                lane.client = CreateClient(lane.executor, deadlineOwner);
            } catch (const std::system_error &) {
                co_return K_RPC_UNAVAILABLE;
            }
        }
        if (needsConnect) {
            std::vector<asio::ip::tcp::endpoint> endpoints{ { address_, port_ } };
            auto budget = std::chrono::ceil<milliseconds>(microseconds(request.deadlineUs - nowUs));
            if (connectTimeoutMs_ > 0) {
                budget = std::min(budget, milliseconds(connectTimeoutMs_));
            }
            auto error = co_await lane.client->connect(host_, service_, budget, &endpoints);
            if (error) {
                lane.client->close();
                co_return error == coro_rpc::errc::timed_out ? K_RPC_DEADLINE_EXCEEDED : K_RPC_UNAVAILABLE;
            }
        }
        const auto sendUs = NowUs();
        const int64_t remainingUs = request.deadlineUs - sendUs;
        if (remainingUs <= 0) {
            co_return K_RPC_DEADLINE_EXCEEDED;
        }

        coro_rpc::request_config_t config{};
        // Pooled calls need a transport timer to release their lane after the caller stops waiting.
        config.request_timeout_duration = deadlineOwner == RequestDeadlineOwner::CallerPump
                                              ? milliseconds(-1)
                                              : std::chrono::ceil<milliseconds>(microseconds(remainingUs));
        config.request_attachment = request.attachment;
        WireRequest wire{ request.workerPort, static_cast<uint64_t>(remainingUs), request.logSampleState,
                          std::move(request.method), std::move(request.traceId), std::move(request.protobuf) };
        lane.lastSendUs = sendUs;
        if (deadlineOwner == RequestDeadlineOwner::CallerPump) {
            auto result = co_await lane.client->call_singleflight<&Dispatcher::InvokeV2>(config, wire);
            co_return FinishExchange(lane, result, state);
        }
        auto future = co_await lane.client->send_request<&Dispatcher::InvokeV2>(config, wire);
        auto result = co_await std::move(future);
        co_return FinishExchange(lane, result, state);
    }

    static StatusCode FinishExchange(Lane &lane, coro_rpc::async_rpc_result<WireReply> &result, CallState &state)
    {
        if (!result) {
            lane.client->close();
            return result.error().code == coro_rpc::errc::timed_out ? K_RPC_DEADLINE_EXCEEDED : K_RPC_UNAVAILABLE;
        }
        return StoreReply(result, state);
    }

    static std::unique_ptr<YltClient> CreateClient(
        coro_io::ExecutorWrapper<> *executor, RequestDeadlineOwner deadlineOwner = RequestDeadlineOwner::Transport)
    {
        YltClient::config config;
        config.max_response_body_bytes = K_MAX_FRAME_BYTES;
        config.max_response_attachment_bytes = kMaxAttachmentBytes;
        if (deadlineOwner == RequestDeadlineOwner::CallerPump) {
            return std::make_unique<YltClient>(executor, std::move(config), YltClient::singleflight_t{});
        }
        return std::make_unique<YltClient>(executor, std::move(config));
    }

    static StatusCode StoreReply(coro_rpc::async_rpc_result<WireReply> &result, CallState &state)
    {
        auto &reply = result->result();
        if (reply.protobuf.size() > kMaxRequestBytes || reply.message.size() > K_MAX_REPLY_MESSAGE_BYTES) {
            return K_RPC_UNAVAILABLE;
        }
        const auto attachmentSize = result->get_attachment().size();
        std::string attachment;
        if (attachmentSize != 0) {
            auto buffer = result->release_buffer();
            auto *data = buffer.resp_attachment_buf_.get_string();
            if (data == nullptr || data->size() < attachmentSize) {
                return K_RPC_UNAVAILABLE;
            }
            data->resize(attachmentSize);
            attachment = std::move(*data);
        }
        state.reply = { reply.code, std::move(reply.message), std::move(reply.protobuf), std::move(attachment) };
        return K_OK;
    }

    const std::string host_;
    asio::ip::address address_;
    bool numericHost_ = false;
    bool callerIo_ = false;
    bool callerIoValid_ = true;
    const uint16_t port_;
    const int32_t connectTimeoutMs_;
    const std::string service_;
    std::array<Lane, K_LANES> lanes_;
    std::array<std::unique_ptr<OwnedLane>, K_LANES> ownedLanes_;
    std::atomic<uint64_t> busy_{ 0 };
    std::once_flag startOnce_;
    std::once_flag ownedStartOnce_;
    std::unique_ptr<coro_io::io_context_pool> pool_;
    std::thread ioThread_;
    std::mutex cleanupMutex_;
    std::condition_variable cleanupReady_;
    uint64_t retiring_ = 0;
    bool cleanupStopping_ = false;
    std::atomic<bool> cleanupFailed_{ false };
    std::thread cleanupThread_;
};

class Server::Impl {
public:
    explicit Impl(Handler handler) : dispatcher_(std::move(handler))
    {
    }

    ~Impl() = default;

    Status Start(const std::string &host, uint16_t port)
    {
        std::lock_guard lock(lifecycleMutex_);
        if (server_ != nullptr) {
            return { K_DUPLICATED, "coro_rpc server already started" };
        }

        auto server = std::make_unique<YltServer>(K_IO_THREADS, port, host, K_IDLE_TIMEOUT);
        server->register_handler<&Dispatcher::InvokeV2>(&dispatcher_, K_INVOKE_V2_METHOD_ID);
        server->connection_admission(
            [this] {
                if (connections_.fetch_add(1, std::memory_order_relaxed) < K_MAX_CONNECTIONS) {
                    return true;
                }
                connections_.fetch_sub(1, std::memory_order_relaxed);
                return false;
            },
            [this] { connections_.fetch_sub(1, std::memory_order_relaxed); });

        dispatcher_.Open();
        auto future = server->async_start();
        if (future.hasResult()) {
            dispatcher_.CloseAndWait();
            return { K_RPC_UNAVAILABLE, "Could not start coro_rpc listener" };
        }
        server_ = std::move(server);
        return Status::OK();
    }

    uint16_t Port() const
    {
        std::lock_guard lock(lifecycleMutex_);
        return server_ == nullptr ? 0 : server_->port();
    }

    void Stop()
    {
        std::lock_guard lock(lifecycleMutex_);
        dispatcher_.CloseAndWait();
        server_.reset();
    }

private:
    Dispatcher dispatcher_;
    mutable std::mutex lifecycleMutex_;
    std::atomic<size_t> connections_{ 0 };
    std::unique_ptr<YltServer> server_;
};

Client::Client(uint16_t port) : Client(K_LOOPBACK, port)
{
}

Client::Client(std::string host, uint16_t port) : Client(std::move(host), port, 0)
{
}

Client::Client(std::string host, uint16_t port, int32_t connectTimeoutMs)
    : impl_(std::make_unique<Impl>(std::move(host), port, connectTimeoutMs))
{
}

Client::~Client() = default;

Status Client::GetInitStatus() const
{
    return impl_->GetInitStatus();
}

Status Client::Call(Request request, Reply &reply)
{
    return impl_->Call(std::move(request), reply);
}

Server::Server(Handler handler) : impl_(std::make_unique<Impl>(std::move(handler)))
{
}

Server::~Server()
{
    Stop();
}

Status Server::Start(uint16_t port)
{
    return Start(K_LOOPBACK, port);
}

Status Server::Start(const std::string &host, uint16_t port)
{
    if (host.empty()) {
        return { K_INVALID, "coro_rpc listener requires a bind address" };
    }
    return impl_->Start(host, port);
}

uint16_t Server::Port() const
{
    return impl_->Port();
}

void Server::Stop()
{
    impl_->Stop();
}
}  // namespace datasystem::coro_unary
