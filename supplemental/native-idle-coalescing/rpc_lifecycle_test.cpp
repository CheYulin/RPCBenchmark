#include <ylt/coro_rpc/coro_rpc_client.hpp>
#include <ylt/coro_rpc/coro_rpc_server.hpp>
#include <async_simple/coro/SyncAwait.h>
#include <chrono>
#include <iostream>
#include <thread>
#include <stdexcept>
using namespace std::chrono_literals;
std::string Echo(const std::string &p) { return p; }
async_simple::coro::Lazy<std::string> Slow(std::string p) {
  co_await coro_io::sleep_for(150ms);
  co_return p;
}
void require(bool value, const char *message) {
  if (!value) throw std::runtime_error(message);
}
int main(int argc, char **argv) {
 try {
  require(argc == 2, "port required");
  auto port = static_cast<unsigned short>(std::stoi(argv[1]));
  int count = 0;
  for (auto policy : {coro_rpc::idle_timeout_policy::per_request,
                      coro_rpc::idle_timeout_policy::coalesced}) {
    coro_rpc::coro_rpc_server server(4, port, "127.0.0.1", 40ms, true, policy);
    server.register_handler<Echo, Slow>();
    auto start = server.async_start();
    require(!start.hasResult(), "server failed to start");
    {
      coro_rpc::coro_rpc_client client;
      require(!async_simple::coro::syncAwait(client.connect("127.0.0.1", std::to_string(port))), "connect");
      std::string payload(4096, 'q');
      for (int i=0; i<100; ++i) {
        auto response = async_simple::coro::syncAwait(client.call<Echo>(payload));
        require(response && response.value() == payload, "echo bytes");
      }
      auto slow = async_simple::coro::syncAwait(client.call<Slow>(payload));
      require(slow && slow.value() == payload, "long handler expired");
      std::this_thread::sleep_for(150ms);
      auto expired = async_simple::coro::syncAwait(client.call_for<Echo>(100ms, payload));
      require(!expired, "idle connection survived");
      ++count;
    }
    {
      coro_rpc::coro_rpc_client client;
      require(!async_simple::coro::syncAwait(client.connect("127.0.0.1", std::to_string(port))), "reconnect");
      auto response = async_simple::coro::syncAwait(client.call<Echo>(std::string("reconnected")));
      require(response && response.value() == "reconnected", "reconnect echo");
      server.stop();
      std::move(start).get();
      ++count;
    }
  }
  std::cout << "RPC_LIFECYCLE_TESTS_PASS " << count << '\n';
  return 0;
 } catch (const std::exception &e) { std::cerr << e.what() << '\n'; return 1; }
}
