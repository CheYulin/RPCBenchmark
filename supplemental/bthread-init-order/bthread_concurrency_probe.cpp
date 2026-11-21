#include <bthread/bthread.h>
#include <brpc/channel.h>

#include <algorithm>
#include <chrono>
#include <cerrno>
#include <condition_variable>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <mutex>
#include <set>
#include <sys/syscall.h>
#include <unistd.h>
#include <vector>

namespace {
bool CopyOwnMaps(const char *destination)
{
    const int input = open("/proc/self/maps", O_RDONLY | O_CLOEXEC);
    const int output = open(destination, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (input < 0 || output < 0) {
        if (input >= 0) close(input);
        if (output >= 0) close(output);
        return false;
    }
    bool okay = true;
    char buffer[8192];
    ssize_t count;
    while ((count = read(input, buffer, sizeof(buffer))) != 0) {
        if (count < 0) { if (errno == EINTR) continue; okay = false; break; }
        ssize_t offset = 0;
        while (offset < count) {
            const auto written = write(output, buffer + offset, count - offset);
            if (written < 0 && errno == EINTR) continue;
            if (written <= 0) { okay = false; break; }
            offset += written;
        }
        if (!okay) break;
    }
    if (close(output) != 0) okay = false;
    if (close(input) != 0) okay = false;
    return okay;
}

int NativeThreadCount()
{
    auto *directory = opendir("/proc/self/task");
    if (directory == nullptr) {
        return -1;
    }
    int count = 0;
    while (auto *entry = readdir(directory)) {
        if (entry->d_name[0] >= '0' && entry->d_name[0] <= '9') {
            ++count;
        }
    }
    closedir(directory);
    return count;
}

void *Noop(void *)
{
    return nullptr;
}

struct Cohort {
    std::mutex mutex;
    std::condition_variable changed;
    std::set<long> nativeThreads;
    int entered = 0;
    int completed = 0;
    bool released = false;

    static void *Call(void *opaque)
    {
        auto &self = *static_cast<Cohort *>(opaque);
        std::unique_lock lock(self.mutex);
        self.nativeThreads.insert(syscall(SYS_gettid));
        ++self.entered;
        self.changed.notify_all();
        self.changed.wait(lock, [&self] { return self.released; });
        ++self.completed;
        return nullptr;
    }
};
}

int main(int argc, char **argv)
{
    if (argc != 3 || argv[2][0] != '/' || (std::strcmp(argv[1], "early8") != 0 && std::strcmp(argv[1], "late8") != 0)) {
        return 2;
    }
    const bool early = std::strcmp(argv[1], "early8") == 0;
    const int entryGetter = bthread_getconcurrency();
    const int entryNative = NativeThreadCount();
    const int earlySet = early ? bthread_setconcurrency(8) : -1;
    const int afterEarlyGetter = bthread_getconcurrency();
    int channelInit;
    {
        brpc::ChannelOptions options;
        brpc::Channel channel;
        channelInit = channel.Init("127.0.0.1:1", &options);
    }
    bthread_t warm{};
    const int warmStart = bthread_start_background(&warm, nullptr, Noop, nullptr);
    int warmJoin = -1;
    if (warmStart == 0) {
        do { warmJoin = bthread_join(warm, nullptr); } while (warmJoin == EINTR);
    }
    const int initializedGetter = bthread_getconcurrency();
    const int lateSet = early ? -1 : bthread_setconcurrency(8);
    const int finalGetter = bthread_getconcurrency();
    const int initializedNative = NativeThreadCount();
    Cohort cohort;
    std::vector<bthread_t> children;
    const int requested = std::clamp(finalGetter, 1, 64);
    int startError = 0;
    for (int i = 0; i < requested; ++i) {
        bthread_t child{};
        const int code = bthread_start_background(&child, nullptr, Cohort::Call, &cohort);
        if (code != 0) {
            startError = code;
            break;
        }
        children.push_back(child);
    }
    {
        std::unique_lock lock(cohort.mutex);
        cohort.changed.wait_for(lock, std::chrono::milliseconds(100), [&] {
            return cohort.entered == static_cast<int>(children.size());
        });
        cohort.released = true;
    }
    cohort.changed.notify_all();
    int joinError = 0;
    for (auto child : children) {
        int code;
        do {
            code = bthread_join(child, nullptr);
        } while (code == EINTR);
        if (code != 0) {
            std::printf("{\"schema\":\"PRIVATE_BTHREAD_CONCURRENCY_AUDIT_V1\","
                        "\"status\":\"AUDIT_JOIN_ERROR\",\"join_return\":%d}\n", code);
            std::fflush(stdout);
            std::_Exit(1);
        }
    }
    const bool mapsCopied = CopyOwnMaps(argv[2]);
    const bool valid = mapsCopied && channelInit == 0 && warmStart == 0 && warmJoin == 0 && startError == 0 &&
                       joinError == 0 && cohort.completed == static_cast<int>(children.size());
    std::printf("{\"schema\":\"PRIVATE_BTHREAD_CONCURRENCY_AUDIT_V1\",\"status\":\"%s\","
                "\"mode\":\"%s\",\"requested_concurrency\":8,\"getter_entry\":%d,"
                "\"early_set_return\":%d,\"getter_after_early\":%d,\"channel_init_return\":%d,"
                "\"warm_start_return\":%d,\"warm_join_return\":%d,\"getter_initialized\":%d,"
                "\"late_set_return\":%d,\"getter_final\":%d,\"callback_start_requested\":%d,"
                "\"callback_started\":%zu,\"callback_completed\":%d,\"callback_start_error\":%d,"
                "\"callback_join_error\":%d,\"native_carriers_observed_lower_bound\":%zu,"
                "\"owned_native_threads_entry\":%d,\"owned_native_threads_initialized\":%d,"
                "\"owned_native_threads_final\":%d,\"actual_carrier_total_proven\":false,"
                "\"own_maps_copied\":%s,\"SDK_executed\":false,\"performance_qualified\":false}\n",
                valid ? "AUDIT_CALLS_COMPLETED" : "AUDIT_API_ERROR", argv[1], entryGetter, earlySet,
                afterEarlyGetter, channelInit, warmStart, warmJoin, initializedGetter, lateSet, finalGetter,
                requested, children.size(), cohort.completed, startError, joinError, cohort.nativeThreads.size(),
                entryNative, initializedNative, NativeThreadCount(), mapsCopied ? "true" : "false");
    return valid ? 0 : 1;
}
