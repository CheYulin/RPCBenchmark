#include "native_bthread_notify_batch.h"
#include "native_notify_diagnostics.h"

#include <asio/io_context.hpp>
#include <asio/post.hpp>
#include <bthread/unstable.h>
#include <ylt/coro_io/io_context_pool.hpp>

#include <exception>

namespace datasystem::coro_unary::diagnostic {
namespace {
struct NativeState {
    unsigned pending = 0;
    bool queued = false;
    NativeBthreadNotifyBatch::FlushFunction flush = nullptr;
};

NativeState &Current()
{
    static thread_local NativeState state;
    return state;
}
}  // namespace

NativeBthreadNotifyBatch::NativeBthreadNotifyBatch() noexcept : NativeBthreadNotifyBatch(Hooks{})
{
}

NativeBthreadNotifyBatch::NativeBthreadNotifyBatch(Hooks hooks) noexcept : hooks_(hooks)
{
    if (!hooks_.start || !hooks_.flush || !hooks_.post) {
        std::terminate();
    }
}

NativeBthreadNotifyBatch::~NativeBthreadNotifyBatch()
{
    if (!accepted_) {
        FlushPrior();
    }
}

bool NativeBthreadNotifyBatch::Eligible(asio::io_context *target) noexcept
{
    return target && *coro_io::get_current() == target
           && target->get_executor().running_in_this_thread() && bthread_self() == INVALID_BTHREAD;
}

void NativeBthreadNotifyBatch::BindFlush() noexcept
{
    auto &state = Current();
    if ((state.pending || state.queued) && state.flush != hooks_.flush) {
        std::terminate();
    }
    state.flush = hooks_.flush;
}

void NativeBthreadNotifyBatch::FlushDefault() noexcept
{
    bthread_flush();
}

void NativeBthreadNotifyBatch::FlushPrior() noexcept
{
    auto &state = Current();
    if (state.pending == 0) {
        return;
    }
    if (bthread_self() != INVALID_BTHREAD || !state.flush) {
        std::terminate();
    }
    state.flush();
    DSCoroDiagRecordNotifyFlush(state.pending);
    state.pending = 0;
}

void NativeBthreadNotifyBatch::PostDefault(asio::io_context &target, NativeId origin)
{
    asio::post(target.get_executor(), [origin] { RunQueued(origin); });
}

void NativeBthreadNotifyBatch::RunQueued(NativeId origin) noexcept
{
    if (std::this_thread::get_id() != origin || bthread_self() != INVALID_BTHREAD) {
        std::terminate();
    }
    auto &state = Current();
    if (!state.queued) {
        std::terminate();
    }
    state.queued = false;
    FlushPrior();
}

void NativeBthreadNotifyBatch::OnAccepted(asio::io_context &target) noexcept
{
    auto &state = Current();
    ++state.pending;
    if (state.pending == BatchLimit) {
        FlushPrior();
    }
    if (!state.queued) {
        state.queued = true;
        try {
            hooks_.post(target, std::this_thread::get_id());
        } catch (...) {
            state.queued = false;
            FlushPrior();
        }
    }
}

NativeBthreadNotifyBatch::Snapshot NativeBthreadNotifyBatch::InspectCurrent() noexcept
{
    const auto &state = Current();
    return { state.pending, state.queued };
}
}  // namespace datasystem::coro_unary::diagnostic
