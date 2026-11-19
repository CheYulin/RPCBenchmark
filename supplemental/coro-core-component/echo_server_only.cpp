#include "coro_unary.h"
#include <bthread/countdown_event.h>
#include <dlfcn.h>
#include <unistd.h>
#include <atomic>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>

extern "C" std::uint64_t DSCoroDiagInlineEchoCount() noexcept;

namespace {
namespace core = datasystem::coro_unary;
constexpr unsigned Owners = 32;
constexpr auto Purpose = "PAIRED_PRIVATE_CORE_MEMORY_ECHO_DISPATCH_SCREEN_ONLY";

void Require(bool ok, const char *reason)
{
    if (!ok) throw std::runtime_error(reason);
}

std::string Quote(const std::string &value)
{
    constexpr char hex[] = "0123456789abcdef";
    std::string out = "\"";
    for (unsigned char c : value) {
        if (c == '\\' || c == '"') { out += '\\'; out += static_cast<char>(c); }
        else if (c < 32) { out += "\\u00"; out += hex[c >> 4]; out += hex[c & 15]; }
        else out += static_cast<char>(c);
    }
    return out + '"';
}

void WriteNew(const std::filesystem::path &path, const std::string &data)
{
    Require(!std::filesystem::exists(path), "evidence file exists");
    std::ofstream out(path); out << data << '\n'; out.close();
    Require(bool(out), "evidence write failed");
}

std::string CoreObject()
{
    Dl_info info{};
    Require(dladdr(reinterpret_cast<void *>(&core::NowUs), &info) != 0 && info.dli_fname,
            "private core object unavailable");
    return info.dli_fname;
}

struct Prime {
    std::atomic<unsigned> entered{0};
    std::atomic<bool> released{false};
    bthread::CountdownEvent gate{1};
    void Release() { if (!released.exchange(true)) gate.signal(); }
};

int Serve(const std::string &host, const std::filesystem::path &ready)
{
    Require(host == "127.0.0.1", "memory-echo screen requires private loopback");
    Prime prime;
    std::atomic<std::uint16_t> port{0};
    core::Server server([&](core::Request request) {
        if (request.workerPort != port.load() || (request.method != "diag.prime" && request.method != "diag.echo"))
            return core::Reply{datasystem::K_INVALID, "diagnostic request mismatch", {}, {}};
        if (request.method == "diag.prime") {
            if (prime.entered.fetch_add(1)+1 == Owners) prime.Release();
            prime.gate.wait();
        }
        return core::Reply{datasystem::K_OK, {}, std::move(request.protobuf), std::move(request.attachment)};
    });
    Require(server.Start(host, 0).IsOk(), "private core server start failed");
    port.store(server.Port());
    std::ostringstream record;
    record << R"({"purpose":)" << Quote(Purpose) << R"(,"pid":)" << getpid()
           << R"(,"port":)" << server.Port() << R"(,"host":)" << Quote(host)
           << R"(,"core_object":)" << Quote(CoreObject())
           << R"(,"core_io_threads":4,"diag_prime_bthread":true,"inline_echo_count_initial":)"
           << DSCoroDiagInlineEchoCount() << R"(,"SDK_Get_Set":false,"RPC30_accepted":false})";
    WriteNew(ready, record.str());
    std::cout << "READY CORE_OWNER_SERVER " << record.str() << std::endl;
    std::string command;
    const bool quit = !std::getline(std::cin, command) || command == "QUIT";
    prime.Release(); server.Stop();
    std::cout << R"(SERVER_STOPPED {"normal_stop":true,"prime_entered":)" << prime.entered.load()
              << R"(,"inline_echo_count":)" << DSCoroDiagInlineEchoCount()
              << R"(,"RPC30_accepted":false})" << std::endl;
    Require(quit, "expected QUIT or EOF");
    return 0;
}
}

int main(int argc, char **argv)
{
    try {
        if (argc == 4 && std::string(argv[1]) == "serve") return Serve(argv[2], argv[3]);
        throw std::invalid_argument("usage: echo-server serve 127.0.0.1 READY_FILE");
    } catch (const std::exception &error) {
        std::cerr << error.what() << '\n';
        return 2;
    }
}
