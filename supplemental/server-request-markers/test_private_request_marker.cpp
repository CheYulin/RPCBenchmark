#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <cstdint>
#include <functional>
#include <iostream>
#include <limits>
#include <new>
#include <string>
#include <thread>
#include <utility>
#include <vector>

void Require(bool condition, const char *expression, int line)
{
    if (!condition) {
        std::cerr << "ASSERTION_FAILED line " << line << ": " << expression << '\n';
        std::exit(1);
    }
}
#define CHECK(condition) Require((condition), #condition, __LINE__)

#ifndef DS_PRIVATE_CORO_REQUEST_MARKERS
#define DS_PRIVATE_CORO_REQUEST_MARKERS 0
#endif
#if DS_PRIVATE_CORO_REQUEST_MARKERS == 1
#include "private_request_marker.h"
namespace private_diagnostic = datasystem::coro_unary::private_diagnostic;
std::atomic<std::uint64_t> g_privateServerRequestId{0};
#endif

struct Event { std::uint32_t id; std::uint32_t phase; };
std::vector<Event> events;
bool observing = true;
bool replyProduced = false;
#if DS_PRIVATE_CORO_REQUEST_MARKERS == 1
extern "C" void DSPrivateCoroServerRequestPhase(std::uint32_t id, std::uint32_t phase) noexcept
{
    if (phase == private_diagnostic::kLambdaEntry) { replyProduced = false; }
    if (phase == private_diagnostic::kReplyReady) { CHECK(replyProduced); }
    if (observing) { events.push_back({id, phase}); }
}
#endif

enum class Submission { Queue, Reject, AllocationFailure };
Submission submission = Submission::Queue;
std::vector<std::function<void()>> queued;
constexpr int K_OK = 0;
constexpr int K_RPC_UNAVAILABLE = 11;
constexpr int K_OUT_OF_MEMORY = 12;
constexpr int K_RUNTIME_ERROR = 13;
struct Reply { int code; int value; };
Reply Failure(int code) { return {code, 0}; }
struct Request { int value; std::string attachment; };
struct Info {
    std::string attachment;
    const std::string &get_request_attachment() const { return attachment; }
    std::string release_request_attachment() { return std::move(attachment); }
};
struct Context {
    Info *info;
    Info *get_context_info() const { return info; }
};

template <typename F> int StartBackgroundTask(void *, F &&task)
{
    if (submission == Submission::Reject) { return 1; }
    if (submission == Submission::AllocationFailure) { throw std::bad_alloc(); }
    queued.emplace_back(std::forward<F>(task));
    return 0;
}

struct Fixture {
    Info info;
    int runCount = 0, responses = 0, finished = 0, lastCode = 0, lastValue = 0;
    bool runError = false;
    bool replyReadyBeforeRespond = false;
    std::string receivedAttachment;
    Reply Run(Request request)
    {
        ++runCount;
        receivedAttachment = std::move(request.attachment);
        replyProduced = true;
        return {runError ? K_RUNTIME_ERROR : K_OK, request.value};
    }
    void Respond(Context, Reply reply) noexcept
    {
#if DS_PRIVATE_CORO_REQUEST_MARKERS == 1
        replyReadyBeforeRespond = !events.empty() && events.back().phase == private_diagnostic::kReplyReady;
#endif
        ++responses; lastCode = reply.code; lastValue = reply.value;
    }
    void Finish() { ++finished; }
    void Submit(int value)
    {
        auto code = K_OK;
        Context context{&info};
        Request request{value, {}};
#include "invoke_admitted_seam.inc"
    }
};

void Reset()
{
    queued.clear(); events.clear(); observing = true; submission = Submission::Queue;
#if DS_PRIVATE_CORO_REQUEST_MARKERS == 1
    g_privateServerRequestId.store(0, std::memory_order_relaxed);
#endif
}

bool Complete(const std::vector<Event> &value)
{
    return value.size() == 3 && value[0].id != 0 && value[0].id == value[1].id &&
           value[0].id == value[2].id && value[0].phase == 1 && value[1].phase == 2 && value[2].phase == 3;
}

#if DS_PRIVATE_CORO_REQUEST_MARKERS == 1
void GeneratorSharedConcurrency()
{
    std::atomic<std::uint64_t> issued{0};
    std::vector<std::vector<std::uint32_t>> values(8);
    std::vector<std::thread> threads;
    for (std::size_t i = 0; i < values.size(); ++i) {
        threads.emplace_back([&, i] {
            for (int n = 0; n < 128; ++n) {
                values[i].push_back(private_diagnostic::NextRequestId(issued, private_diagnostic::kRequestIdCeiling));
            }
        });
    }
    for (auto &thread : threads) { thread.join(); }
    std::vector<std::uint32_t> all;
    for (const auto &part : values) { all.insert(all.end(), part.begin(), part.end()); }
    std::sort(all.begin(), all.end());
    CHECK(all.size() == 1024);
    for (std::size_t i = 0; i < all.size(); ++i) { CHECK(all[i] == i + 1); }
}

void GeneratorFiniteBounds()
{
    std::atomic<std::uint64_t> issued{0};
    for (std::uint32_t n = 1; n <= 3; ++n) { CHECK(private_diagnostic::NextRequestId(issued, 3) == n); }
    for (int n = 0; n < 100; ++n) { CHECK(private_diagnostic::NextRequestId(issued, 3) == 0); }
    CHECK(issued.load() == 3);
    issued.store(std::numeric_limits<std::uint64_t>::max());
    CHECK(private_diagnostic::NextRequestId(issued, 65535) == 0);
    CHECK(issued.load() == std::numeric_limits<std::uint64_t>::max());
    issued.store(0);
    CHECK(private_diagnostic::NextRequestId(issued, 0) == 0 && issued.load() == 0);
    issued.store(std::numeric_limits<std::uint32_t>::max() - 1ULL);
    CHECK(private_diagnostic::NextRequestId(issued, std::numeric_limits<std::uint32_t>::max()) ==
           std::numeric_limits<std::uint32_t>::max());
    CHECK(private_diagnostic::NextRequestId(issued, std::numeric_limits<std::uint32_t>::max()) == 0);
}

void CapturedAcrossDispatchers()
{
    Reset(); Fixture first, second;
    first.info.attachment = "synthetic";
    first.Submit(21); second.Submit(34);
    CHECK(events.size() == 2 && events[0].id == 1 && events[1].id == 2);
    CHECK(private_diagnostic::NextRequestId(g_privateServerRequestId, 65535) == 3);
    queued[1](); queued[0]();
    const std::vector<Event> expected{{1,1},{2,1},{2,2},{2,3},{1,2},{1,3}};
    CHECK(events.size() == expected.size());
    for (std::size_t i = 0; i < expected.size(); ++i) {
        CHECK(events[i].id == expected[i].id && events[i].phase == expected[i].phase);
    }
    CHECK(first.lastValue == 21 && second.lastValue == 34 && first.finished == 1 && second.finished == 1);
    CHECK(first.receivedAttachment == "synthetic");
}

void SubmissionFailuresRetainIncomplete()
{
    for (auto mode : {Submission::Reject, Submission::AllocationFailure}) {
        Reset(); submission = mode; Fixture value; value.Submit(8);
        CHECK(events.size() == 1 && events[0].phase == 1 && events[0].id == 1);
        CHECK(!Complete(events) && queued.empty() && value.finished == 1 && value.responses == 1);
        CHECK(value.runCount == 0 && value.lastCode == (mode == Submission::Reject ? K_RPC_UNAVAILABLE : K_OUT_OF_MEMORY));
    }
}

void ObserverCancellationRetainsPrefix()
{
    Reset(); Fixture value; value.Submit(8); observing = false; queued[0]();
    CHECK(events.size() == 1 && !Complete(events));
    CHECK(value.runCount == 1 && value.finished == 1 && value.responses == 1 && value.lastCode == K_OK);
}

void OverflowNeverChangesBusinessResponse()
{
    Reset(); g_privateServerRequestId.store(65535); Fixture value; value.Submit(8); queued[0]();
    CHECK(events.size() == 3 && !Complete(events));
    for (const auto &event : events) { CHECK(event.id == 0); }
    CHECK(value.runCount == 1 && value.finished == 1 && value.responses == 1 && value.lastCode == K_OK);
    CHECK(g_privateServerRequestId.load() == 65535);
}

void ReplyReadyPrecedesRespondForErrorReply()
{
    Reset(); Fixture value; value.runError = true; value.Submit(8); queued[0]();
    CHECK(Complete(events) && value.lastCode == K_RUNTIME_ERROR);
    CHECK(value.replyReadyBeforeRespond);
    CHECK(value.finished == 1 && value.responses == 1);
}
#endif

int main()
{
#if DS_PRIVATE_CORO_REQUEST_MARKERS == 1
    GeneratorSharedConcurrency(); GeneratorFiniteBounds(); CapturedAcrossDispatchers();
    SubmissionFailuresRetainIncomplete(); ObserverCancellationRetainsPrefix();
    OverflowNeverChangesBusinessResponse(); ReplyReadyPrecedesRespondForErrorReply();
    std::cout << "7 enabled cases passed\n";
#else
    Reset(); Fixture value; value.Submit(8); queued[0]();
    CHECK(events.empty() && value.finished == 1 && value.responses == 1 && value.lastValue == 8);
    std::cout << "1 default-off business seam passed\n";
#endif
}
