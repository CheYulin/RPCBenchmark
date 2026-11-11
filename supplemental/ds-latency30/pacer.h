#pragma once

#include <algorithm>
#include <cstdint>
#include <limits>
#include <map>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace arrival {
using Ns = std::int64_t;
using Wide = unsigned __int128;
constexpr std::uint64_t kSecond = 1000000000;
constexpr std::size_t kMaxArrivals = 2000000;

inline Ns checkedTime(__int128 value)
{
    if (value < 0 || value > std::numeric_limits<Ns>::max()) throw std::invalid_argument("time overflow");
    return static_cast<Ns>(value);
}

struct Plan {
    Plan(Ns epoch, Ns duration, std::uint64_t rate, Ns budget)
        : epoch(epoch), duration(duration), budget(budget), rate(rate)
    {
        if (epoch < 0 || duration <= 0 || budget <= 0 || rate == 0 || rate > kSecond) {
            throw std::invalid_argument("invalid arrival plan");
        }
        const Wide total = (Wide(duration) * rate + kSecond - 1) / kSecond;
        if (total == 0 || total > kMaxArrivals) throw std::invalid_argument("arrival record cap exceeded");
        count = static_cast<std::size_t>(total);
        end = checkedTime(__int128(epoch) + duration);
        checkedTime(__int128(end) + budget);
    }
    Ns scheduled(std::size_t id) const
    {
        if (id >= count) throw std::out_of_range("arrival id");
        return checkedTime(__int128(epoch) + static_cast<__int128>(Wide(id) * kSecond / rate));
    }
    Ns deadline(std::size_t id) const { return checkedTime(__int128(scheduled(id)) + budget); }
    Ns epoch, duration, budget, end;
    std::uint64_t rate;
    std::size_t count;
};

enum class State { Unissued, Rejected, Queued, Reserved, Inflight, Expired, Timely, Late, Error, Mismatch };
enum class Rejection { None, QueueFull, GenerationEnded };
enum class Outcome { Success, RpcError, Mismatch };

inline const char* name(State state)
{
    switch (state) {
        case State::Unissued: return "producer_unissued";
        case State::Rejected: return "queue_rejected";
        case State::Queued: return "pending_in_queue";
        case State::Reserved: return "pending_reserved";
        case State::Inflight: return "pending_inflight";
        case State::Expired: return "expired_in_queue";
        case State::Timely: return "timely_success";
        case State::Late: return "late_success";
        case State::Error: return "rpc_error";
        case State::Mismatch: return "mismatch";
    }
    throw std::logic_error("invalid state");
}

struct Record {
    State state = State::Unissued;
    Rejection rejection = Rejection::None;
    Ns scheduled = -1, deadline = -1, observed = -1, enqueue = -1, dequeue = -1;
    Ns dispatch = -1, start = -1, end = -1, validated = -1;
    int worker = -1, statusCode = 0;
};

struct Counts {
    std::uint64_t planned = 0, observed = 0, unissued = 0, admitted = 0, rejected = 0;
    std::uint64_t dispatched = 0, expired = 0, pendingQueue = 0, pendingInflight = 0;
    std::uint64_t timely = 0, late = 0, errors = 0, mismatches = 0;
    bool conserved() const
    {
        return planned == observed + unissued && observed == admitted + rejected
            && admitted == dispatched + expired + pendingQueue
            && dispatched == timely + late + errors + mismatches + pendingInflight;
    }
    bool valid() const
    {
        return conserved() && planned > 0 && timely == planned && !unissued && !rejected && !expired
            && !pendingQueue && !pendingInflight && !late && !errors && !mismatches;
    }
};

class FixedQueue {
public:
    explicit FixedQueue(std::size_t capacity) : slots(validate(capacity)) {}
    bool empty() const { return used == 0; }
    std::size_t size() const { return used; }
    std::size_t front() const { if (!used) throw std::logic_error("empty FIFO"); return slots[head]; }
    void pop_front() { if (!used) throw std::logic_error("empty FIFO"); head = (head + 1) % slots.size(); --used; }
    void push_back(std::size_t id)
    {
        if (used == slots.size()) throw std::logic_error("full FIFO");
        slots[(head + used++) % slots.size()] = id;
    }
private:
    static std::size_t validate(std::size_t capacity)
    {
        if (!capacity || capacity > 65536) throw std::invalid_argument("FIFO capacity");
        return capacity;
    }
    std::vector<std::size_t> slots;
    std::size_t head = 0, used = 0;
};

// Caller supplies synchronization; this state machine performs no clock reads or RPCs.
class Ledger {
public:
    Ledger(Plan plan, std::size_t capacity, std::size_t callers, Ns latenessLimit, std::size_t burstLimit)
        : plan(std::move(plan)), capacity(capacity), callers(callers), latenessLimit(latenessLimit), burstLimit(burstLimit),
          records(this->plan.count), queue(capacity)
    {
        if (!capacity || capacity > 65536 || !callers || callers > 128 || latenessLimit < 0 || !burstLimit) {
            throw std::invalid_argument("invalid queue/caller/generator bound");
        }
        for (std::size_t id = 0; id < records.size(); ++id) {
            records[id].scheduled = this->plan.scheduled(id);
            records[id].deadline = this->plan.deadline(id);
        }
    }
    bool observe(std::size_t id, Ns observed, Ns enqueue)
    {
        if (id != next || id >= records.size()) throw std::logic_error("observation order/duplicate");
        auto& record = records[id];
        if (observed < record.scheduled || enqueue < observed) throw std::logic_error("invalid producer time");
        ++next;
        record.observed = observed;
        maximumLateness = std::max(maximumLateness, enqueue - record.scheduled);
        generatorInvalid |= enqueue - record.scheduled > latenessLimit;
        if (enqueue >= plan.end || queue.size() == capacity) {
            record.state = State::Rejected;
            record.rejection = enqueue >= plan.end ? Rejection::GenerationEnded : Rejection::QueueFull;
            return false;
        }
        record.enqueue = enqueue;
        record.state = State::Queued;
        queue.push_back(id);
        queueHighWater = std::max(queueHighWater, queue.size());
        return true;
    }
    void burst(std::size_t due)
    {
        maximumBurst = std::max(maximumBurst, due);
        generatorInvalid |= due > burstLimit;
    }
    bool reserve(Ns now, int worker, std::size_t& id)
    {
        if (queue.empty()) return false;
        id = queue.front();
        queue.pop_front();
        auto& record = records[id];
        record.dequeue = now;
        record.worker = worker;
        if (now < record.enqueue) throw std::logic_error("dequeue before enqueue");
        record.state = State::Reserved;
        return true;
    }
    bool dispatch(std::size_t id, Ns now)
    {
        auto& record = records.at(id);
        if (record.state != State::Reserved || now < record.dequeue) throw std::logic_error("invalid dispatch");
        if (now >= record.deadline) {
            record.state = State::Expired;
            return false;
        }
        if (inflight == callers) throw std::logic_error("caller admission exceeded");
        record.dispatch = now;
        record.state = State::Inflight;
        ++inflight;
        inflightHighWater = std::max(inflightHighWater, inflight);
        return true;
    }
    void expireBeforeApi(std::size_t id, Ns now)
    {
        auto& record = records.at(id);
        if (record.state != State::Inflight || now < record.deadline) throw std::logic_error("invalid expiry");
        record.state = State::Expired;
        --inflight;
    }
    void complete(std::size_t id, Ns start, Ns end, Ns validated, Outcome outcome, int statusCode)
    {
        auto& record = records.at(id);
        if (record.state != State::Inflight || start < record.dispatch || end < start || validated < end) {
            throw std::logic_error("invalid/duplicate completion");
        }
        record.start = start;
        record.end = end;
        record.validated = validated;
        record.statusCode = statusCode;
        record.state = outcome == Outcome::RpcError ? State::Error : outcome == Outcome::Mismatch ? State::Mismatch
            : end > record.deadline ? State::Late : State::Timely;
        --inflight;
    }
    Counts counts() const
    {
        Counts result;
        result.planned = records.size();
        for (const auto& record : records) {
            if (record.state == State::Unissued) { ++result.unissued; continue; }
            ++result.observed;
            if (record.state == State::Rejected) { ++result.rejected; continue; }
            ++result.admitted;
            if (record.state == State::Queued || record.state == State::Reserved) { ++result.pendingQueue; continue; }
            if (record.state == State::Expired) { ++result.expired; continue; }
            ++result.dispatched;
            switch (record.state) {
                case State::Inflight: ++result.pendingInflight; break;
                case State::Timely: ++result.timely; break;
                case State::Late: ++result.late; break;
                case State::Error: ++result.errors; break;
                case State::Mismatch: ++result.mismatches; break;
                default: throw std::logic_error("invalid count state");
            }
        }
        if (!result.conserved()) throw std::logic_error("accounting conservation failure");
        return result;
    }
    Plan plan;
    const std::size_t capacity, callers;
    const Ns latenessLimit;
    const std::size_t burstLimit;
    std::vector<Record> records;
    FixedQueue queue;
    std::size_t next = 0, inflight = 0, queueHighWater = 0, inflightHighWater = 0, maximumBurst = 0;
    Ns maximumLateness = 0;
    bool generatorInvalid = false;
};

inline std::string decimal(Wide value)
{
    if (!value) return "0";
    std::string text;
    while (value) { text.push_back(static_cast<char>('0' + value % 10)); value /= 10; }
    std::reverse(text.begin(), text.end());
    return text;
}

struct Histogram {
    void add(std::uint64_t ns)
    {
        auto us = ns / 1000 + (ns % 1000 != 0);
        auto step = std::uint64_t{1};
        while (us / step > 2048) step *= 2;
        const auto upper = (us / step + (us % step != 0)) * step;
        ++bins[upper];
        ++count;
        sumNs += ns;
        minimumNs = std::min(minimumNs, ns);
        maximumNs = std::max(maximumNs, ns);
    }
    void merge(const Histogram& other)
    {
        for (const auto& bin : other.bins) bins[bin.first] += bin.second;
        count += other.count;
        sumNs += other.sumNs;
        minimumNs = std::min(minimumNs, other.minimumNs);
        maximumNs = std::max(maximumNs, other.maximumNs);
    }
    std::uint64_t percentile(unsigned percent) const
    {
        if (!count || percent < 1 || percent > 100) throw std::invalid_argument("invalid percentile");
        const auto rank = (Wide(count) * percent + 99) / 100;
        Wide seen = 0;
        for (const auto& bin : bins) { seen += bin.second; if (seen >= rank) return bin.first; }
        throw std::logic_error("invalid histogram count");
    }
    std::uint64_t count = 0, minimumNs = std::numeric_limits<std::uint64_t>::max(), maximumNs = 0;
    Wide sumNs = 0;
    std::map<std::uint64_t, std::uint64_t> bins;
};
}  // namespace arrival
