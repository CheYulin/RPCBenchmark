#include "lab.h"
#include <ylt/coro_rpc/coro_rpc_server.hpp>
#include <iostream>
#include <stdexcept>
#include <string_view>
#include <vector>
namespace {
std::string Echo(const std::string &payload) { return payload; }
}
int main(int argc, char **argv) {
 try {
  int mode = -1;
  std::vector<char *> args;
  for (int i=0;i<argc;++i) {
    if (std::string_view(argv[i]) == "--idle-coalesced") {
      if (mode != -1 || i+1 == argc) throw std::invalid_argument("idle policy argument");
      const std::string_view value(argv[++i]);
      if (value != "0" && value != "1") throw std::invalid_argument("idle policy value");
      mode = value == "1";
    } else args.push_back(argv[i]);
  }
  if (mode == -1) throw std::invalid_argument("idle policy required");
  const auto options=lab::parse(args.size(), args.data());
  if (!options.server || options.handler_workers || options.handler_us) throw std::invalid_argument("echo server only");
  lab::blockShutdownSignals();
  const auto policy=mode ? coro_rpc::idle_timeout_policy::coalesced : coro_rpc::idle_timeout_policy::per_request;
  coro_rpc::coro_rpc_server server(options.workers, options.port, "127.0.0.1", std::chrono::seconds(5), true, policy);
  server.register_handler<Echo>();
  auto completion=server.async_start();
  if (completion.hasResult()) throw std::runtime_error("listen failed");
  std::cerr << "SERVER_IDLE_COALESCED " << mode << " IDLE_TIMEOUT_MS 5000\n";
  lab::ready();lab::waitForShutdown();server.stop();std::move(completion).get();return 0;
 } catch(const std::exception &e) { std::cerr << e.what() << '\n'; return 1; }
}
