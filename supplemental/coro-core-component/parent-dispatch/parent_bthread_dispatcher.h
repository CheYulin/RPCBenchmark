#pragma once

#include <bthread/bthread.h>
#include <cstddef>
#include <functional>
#include <memory>
#include <utility>

#include "datasystem/utils/status.h"

namespace datasystem::coro_unary::diagnostic {
class ParentBthreadDispatcher {
public:
    static constexpr std::size_t Capacity = 64;

    // Job owns all state needed by its asynchronous child until after child join.
    struct Job {
        virtual ~Job() noexcept = default;
        virtual void Run() = 0;
        virtual void ChildStartFailed(StatusCode code) noexcept = 0;
        virtual void RunFailed(StatusCode code) noexcept = 0;
    };

    struct Owner {
        virtual ~Owner() = default;
        virtual void JobRetired() noexcept = 0;
    };

    using StartFunction = int (*)(bthread_t *, const bthread_attr_t *, void *(*)(void *), void *);
    struct Hooks {
        StartFunction parent = &bthread_start_background;
        StartFunction child = &bthread_start_urgent;
    };

    struct Snapshot {
        std::size_t parents = 0;
        std::size_t occupied = 0;
        std::size_t constructing = 0;
        bool accepting = false;
        bool stopping = false;
    };

    // Owner must outlive Stop; factories, jobs and retirement callbacks cannot call Start/Stop.
    explicit ParentBthreadDispatcher(Owner &owner);
    ParentBthreadDispatcher(Owner &owner, Hooks hooks);
    ~ParentBthreadDispatcher();
    ParentBthreadDispatcher(const ParentBthreadDispatcher &) = delete;
    ParentBthreadDispatcher &operator=(const ParentBthreadDispatcher &) = delete;

    StatusCode Start();
    void Stop() noexcept;
    // An empty snapshot is not proof that parent retirement callbacks have exited.
    Snapshot Inspect() const;

    // The factory runs synchronously after reservation and is never retained.
    // Rejection leaves admission cleanup to the caller; accepted jobs retire once.
    template <typename Factory>
    StatusCode TrySubmit(Factory &&factory)
    {
        auto make = [&factory]() -> std::unique_ptr<Job> {
            return std::invoke(std::forward<Factory>(factory));
        };
        return SubmitFactory(&make, [](void *pointer) -> std::unique_ptr<Job> {
            return (*static_cast<decltype(make) *>(pointer))();
        });
    }

private:
    using FactoryFunction = std::unique_ptr<Job> (*)(void *);
    StatusCode SubmitFactory(void *context, FactoryFunction factory);
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
}
