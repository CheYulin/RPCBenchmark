#pragma once
#include <cstddef>
#include <stdexcept>
#include <vector>

// Negative policy model: the previous driver counted registration as ready, before the new priming requirement.
class LegacyRegisteredReady {
public:
    explicit LegacyRegisteredReady(std::size_t count) : states(count) {}
    void Register(std::size_t caller)
    {
        if (states.at(caller)) throw std::logic_error("duplicate registration");
        states[caller] = 1;
        ++registered;
    }
    void Complete(std::size_t caller, bool)
    {
        if (states.at(caller) != 1) throw std::logic_error("invalid completion");
        states[caller] = 2;
    }
    bool AllComplete() const { return registered == states.size(); }
    bool ClaimReady()
    {
        if (!AllComplete() || released) return false;
        released = true;
        return true;
    }
private:
    std::vector<unsigned char> states;
    std::size_t registered = 0;
    bool released = false;
};
