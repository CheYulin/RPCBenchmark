#pragma once

#include <bthread/bthread.h>

#include <exception>
#include <memory>
#include <thread>
#include <type_traits>
#include <utility>

#include "datasystem/common/rpc/bthread_utils.h"

namespace asio {
class io_context;
}

namespace datasystem::coro_unary::diagnostic {
class NativeBthreadNotifyBatch {
public:
    using NativeId = std::thread::id;
    using StartFunction = int (*)(bthread_t *, const bthread_attr_t *, void *(*)(void *), void *);
    using FlushFunction = void (*)() noexcept;
    using PostFunction = void (*)(asio::io_context &, NativeId);
    static constexpr unsigned BatchLimit = 16;

    struct Hooks {
        StartFunction start = &bthread_start_background;
        FlushFunction flush = &FlushDefault;
        PostFunction post = &PostDefault;
    };

    struct Snapshot {
        unsigned pending;
        bool queued;
    };

    NativeBthreadNotifyBatch() noexcept;
    explicit NativeBthreadNotifyBatch(Hooks hooks) noexcept;
    ~NativeBthreadNotifyBatch();
    NativeBthreadNotifyBatch(const NativeBthreadNotifyBatch &) = delete;
    NativeBthreadNotifyBatch &operator=(const NativeBthreadNotifyBatch &) = delete;

    // One synchronous attempt; target is borrowed only until Start returns.
    template <typename F>
    int Start(asio::io_context *target, bthread_t *tid, F &&fn)
    {
        if (started_) {
            std::terminate();
        }
        started_ = true;
        if (!Eligible(target)) {
            FlushPrior();
            const int code = datasystem::StartBackgroundTask(tid, std::forward<F>(fn));
            accepted_ = code == 0;
            return code;
        }
        BindFlush();
        using Function = std::decay_t<F>;
        try {
            auto function = std::make_unique<Function>(std::forward<F>(fn));
            auto attr = BTHREAD_ATTR_NORMAL;
            attr.flags |= BTHREAD_NOSIGNAL;
            bthread_t placeholder{};
            const int code = hooks_.start(tid ? tid : &placeholder, &attr, [](void *argument) -> void * {
                auto *call = static_cast<Function *>(argument);
                (*call)();
                delete call;
                return nullptr;
            }, function.get());
            if (code != 0) {
                FlushPrior();
                return code;
            }
            function.release();
            accepted_ = true;
            OnAccepted(*target);
            return 0;
        } catch (...) {
            FlushPrior();
            throw;
        }
    }

    // Rejection/factory failure flushes prior work on this same native producer.
    static void FlushPrior() noexcept;
    static Snapshot InspectCurrent() noexcept;
    static void RunQueued(NativeId origin) noexcept;

private:
    static bool Eligible(asio::io_context *target) noexcept;
    static void FlushDefault() noexcept;
    static void PostDefault(asio::io_context &target, NativeId origin);
    void BindFlush() noexcept;
    void OnAccepted(asio::io_context &target) noexcept;

    const Hooks hooks_;
    bool started_ = false;
    bool accepted_ = false;
};
}  // namespace datasystem::coro_unary::diagnostic
