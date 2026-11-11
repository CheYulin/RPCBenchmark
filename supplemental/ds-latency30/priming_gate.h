#pragma once

#include <cstddef>
#include <stdexcept>
#include <vector>

namespace arrival {

// The driver protects this startup-only state and its per-caller proof with Shared::mutex.
class CallerReadiness {
public:
    explicit CallerReadiness(std::size_t count)
    {
        if (count == 0 || count > 128) throw std::invalid_argument("invalid caller count");
        states_.resize(count);
    }

    void Register(std::size_t caller)
    {
        if (states_.at(caller) != 0) throw std::logic_error("duplicate caller registration");
        states_[caller] = 1;
    }

    void Complete(std::size_t caller, bool success)
    {
        if (states_.at(caller) != 1) throw std::logic_error("invalid caller completion");
        states_[caller] = 2;
        ++completed_;
        failed_ |= !success;
    }

    bool AllComplete() const { return completed_ == states_.size(); }

    bool ClaimReady()
    {
        if (!AllComplete() || failed_ || released_) return false;
        released_ = true;
        return true;
    }

private:
    std::vector<unsigned char> states_;
    std::size_t completed_ = 0;
    bool failed_ = false, released_ = false;
};

}  // namespace arrival
