#include "parent_bthread_dispatcher.h"
#include <bthread/condition_variable.h>
#include <array>
#include <bit>
#include <cerrno>
#include <cstdint>
#include <exception>
#include <mutex>
#include <new>

namespace datasystem::coro_unary::diagnostic {
struct ParentBthreadDispatcher::Impl {
    enum class State { Idle, Reserved, Ready, Running };
    struct Slot {
        Impl *owner = nullptr;
        std::size_t index = 0;
        bthread_t parent = 0;
        bthread::ConditionVariable ready;
        State state = State::Idle;
        std::unique_ptr<Job> job;
    };

    explicit Impl(Owner &value, Hooks functions) : observer(value), hooks(functions)
    {
        for (std::size_t i = 0; i < Capacity; ++i) {
            slots[i].owner = this;
            slots[i].index = i;
        }
    }

    static void Join(bthread_t id) noexcept
    {
        int code;
        do {code = bthread_join(id, nullptr);} while (code == EINTR);
        if (code != 0) std::terminate();
    }

    static void *RunParent(void *argument)
    {
        auto &slot = *static_cast<Slot *>(argument);
        slot.owner->Run(slot);
        return nullptr;
    }

    static void *RunChild(void *argument) noexcept
    {
        auto &job = *static_cast<Job *>(argument);
        try {job.Run();}
        catch (const std::bad_alloc &) {job.RunFailed(K_OUT_OF_MEMORY);}
        catch (...) {job.RunFailed(K_RUNTIME_ERROR);}
        return nullptr;
    }

    void Run(Slot &slot)
    {
        for (;;) {
            std::unique_ptr<Job> job;
            {
                std::unique_lock lock(mutex);
                while (slot.state != State::Ready && !(stopping && slot.state == State::Idle)) {
                    slot.ready.wait(lock);
                }
                if (slot.state == State::Idle) return;
                job = std::move(slot.job);
                slot.state = State::Running;
            }
            bthread_t child = 0;
            const auto attr = BTHREAD_ATTR_NORMAL;
            int code;
            try {code = hooks.child(&child, &attr, RunChild, job.get());}
            catch (const std::bad_alloc &) {code = ENOMEM;}
            catch (...) {code = EINVAL;}
            if (code == 0) {
                if (child == 0) std::terminate();
                Join(child);
            } else {
                job->ChildStartFailed(code == ENOMEM ? K_OUT_OF_MEMORY : K_RPC_UNAVAILABLE);
            }
            job.reset();
            {
                std::lock_guard lock(mutex);
                slot.state = State::Idle;
                free |= std::uint64_t{1} << slot.index;
            }
            // A new job may already reserve this slot; never clear it after Finish.
            observer.JobRetired();
        }
    }

    StatusCode Start()
    {
        std::lock_guard lifecycleLock(lifecycle);
        {
            std::lock_guard lock(mutex);
            if (started != 0 || accepting || stopping) return K_RUNTIME_ERROR;
            if (!hooks.parent || !hooks.child) return K_INVALID;
        }
        const auto attr = BTHREAD_ATTR_NORMAL;
        for (auto &slot : slots) {
            int code;
            try {code = hooks.parent(&slot.parent, &attr, RunParent, &slot);}
            catch (const std::bad_alloc &) {code = ENOMEM;}
            catch (...) {code = EINVAL;}
            if (code != 0) {
                StopStarted();
                return code == ENOMEM ? K_OUT_OF_MEMORY : K_RPC_UNAVAILABLE;
            }
            if (slot.parent == 0) std::terminate();
            std::lock_guard lock(mutex);
            ++started;
        }
        {
            std::lock_guard lock(mutex);
            accepting = true;
        }
        return K_OK;
    }

    void StopStarted() noexcept
    {
        std::size_t count;
        {
            std::lock_guard lock(mutex);
            accepting = false;
            stopping = true;
            count = started;
            for (auto &slot : slots) slot.ready.notify_all();
        }
        for (std::size_t i = 0; i < count; ++i) Join(slots[i].parent);
        {
            std::lock_guard lock(mutex);
            if (free != ~std::uint64_t{0} || constructing != 0) std::terminate();
            for (auto &slot : slots) {
                if (slot.state != State::Idle || slot.job) std::terminate();
                slot.parent = 0;
            }
            started = 0;
            stopping = false;
        }
    }

    void Stop() noexcept
    {
        std::lock_guard lock(lifecycle);
        StopStarted();
    }

    void Cancel(std::size_t index) noexcept
    {
        std::lock_guard lock(mutex);
        auto &slot = slots[index];
        if (slot.state != State::Reserved) std::terminate();
        slot.state = State::Idle;
        --constructing;
        free |= std::uint64_t{1} << index;
        slot.ready.notify_one();
    }

    StatusCode Submit(void *context, FactoryFunction factory)
    {
        std::size_t index;
        {
            std::lock_guard lock(mutex);
            if (!accepting) return K_SHUTTING_DOWN;
            if (free == 0) return K_TRY_AGAIN;
            index = static_cast<std::size_t>(std::countr_zero(free));
            free &= ~(std::uint64_t{1} << index);
            slots[index].state = State::Reserved;
            ++constructing;
        }
        std::unique_ptr<Job> job;
        try {job = factory(context);}
        catch (const std::bad_alloc &) {Cancel(index); return K_OUT_OF_MEMORY;}
        catch (...) {Cancel(index); return K_RUNTIME_ERROR;}
        if (!job) {Cancel(index); return K_INVALID;}
        {
            std::lock_guard lock(mutex);
            auto &slot = slots[index];
            if (slot.state != State::Reserved) std::terminate();
            slot.job = std::move(job);
            slot.state = State::Ready;
            --constructing;
            slot.ready.notify_one();
        }
        return K_OK;
    }

    Snapshot Inspect() const
    {
        std::lock_guard lock(mutex);
        return {started, Capacity - static_cast<std::size_t>(std::popcount(free)), constructing, accepting, stopping};
    }

    Owner &observer;
    const Hooks hooks;
    bthread::Mutex lifecycle;
    mutable bthread::Mutex mutex;
    std::array<Slot, Capacity> slots;
    std::uint64_t free = ~std::uint64_t{0};
    std::size_t started = 0, constructing = 0;
    bool accepting = false, stopping = false;
};

ParentBthreadDispatcher::ParentBthreadDispatcher(Owner &owner) : ParentBthreadDispatcher(owner, Hooks{}) {}
ParentBthreadDispatcher::ParentBthreadDispatcher(Owner &owner, Hooks hooks) : impl_(std::make_unique<Impl>(owner, hooks)) {}
ParentBthreadDispatcher::~ParentBthreadDispatcher() {impl_->Stop();}
StatusCode ParentBthreadDispatcher::Start() {return impl_->Start();}
void ParentBthreadDispatcher::Stop() noexcept {impl_->Stop();}
ParentBthreadDispatcher::Snapshot ParentBthreadDispatcher::Inspect() const {return impl_->Inspect();}
StatusCode ParentBthreadDispatcher::SubmitFactory(void *context, FactoryFunction factory) {return impl_->Submit(context, factory);}
}
