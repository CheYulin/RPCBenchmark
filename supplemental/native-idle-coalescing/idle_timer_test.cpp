#include <ylt/coro_rpc/impl/coro_connection.hpp>
#include <asio/ip/tcp.hpp>
#include <dirent.h>
#include <chrono>
#include <iostream>
#include <memory>
#include <stdexcept>
using namespace std::chrono_literals;
using Policy = coro_rpc::idle_timeout_policy;
void require(bool ok, const char *message) { if (!ok) throw std::runtime_error(message); }
struct Pair {
  asio::io_context io;
  coro_io::ExecutorWrapper<> executor{io.get_executor()};
  asio::ip::tcp::acceptor acceptor{io, {asio::ip::make_address("127.0.0.1"), 0}};
  asio::ip::tcp::socket peer{io};
  std::shared_ptr<coro_rpc::coro_connection> connection;
  unsigned closed = 0;
  std::chrono::steady_clock::time_point closed_at{};
  explicit Pair(Policy policy, std::chrono::steady_clock::duration duration = 40ms) {
    peer.connect(acceptor.local_endpoint());
    asio::ip::tcp::socket accepted{io}; acceptor.accept(accepted);
    coro_io::socket_wrapper_t wrapper{std::move(accepted), &executor};
    connection = std::make_shared<coro_rpc::coro_connection>(std::move(wrapper), duration, policy);
    connection->set_quit_callback([this](auto) { ++closed; closed_at = std::chrono::steady_clock::now(); }, 1);
  }
  void pump(std::chrono::steady_clock::duration duration) { io.restart(); io.run_for(duration); }
  ~Pair() {
    if (connection) connection->async_close();
    connection.reset();
    io.restart(); io.run_for(6s);
    if (!io.stopped()) std::_Exit(91);
  }
};
int fds() { auto *d = opendir("/proc/self/fd"); require(d, "fd inventory"); int n = 0;
  while (auto *e = readdir(d)) if (e->d_name[0] != '.') ++n; closedir(d); return n; }
int main() { try {
  { Pair warm(Policy::per_request, 1ms); warm.connection->test_idle_reset(); warm.pump(100ms); }
  const int baseline = fds(); int count = 0;
  { Pair p(Policy::coalesced); for (int i=0;i<1000;++i) { p.connection->test_idle_reset(); p.connection->test_idle_payload(); p.connection->test_idle_response(); }
    require(p.connection->test_idle_wait_count()==1,"coalesced repeated refresh must keep one wait");
    auto deadline=p.connection->test_idle_deadline(); p.pump(500ms);
    require(p.closed==1 && p.closed_at>=deadline,"closes after refreshed deadline"); ++count; }
  { Pair p(Policy::per_request); for (int i=0;i<8;++i) { p.connection->test_idle_reset(); p.connection->test_idle_payload(); p.connection->test_idle_response(); }
    require(p.connection->test_idle_wait_count()==16,"default timer rearm path unchanged");p.pump(500ms);require(p.closed==1,"default expiry");++count; }
  { Pair p(Policy::coalesced);p.connection->test_idle_reset();p.connection->test_idle_payload();p.pump(150ms);
    require(p.closed==0 && p.connection->test_idle_wait_count()==1,"long handler survives idle expiry");
    p.connection->test_idle_response();require(p.connection->test_idle_wait_count()==2,"last response rearms inactive monitor");p.pump(500ms);require(p.closed==1,"idle expiry after long handler");++count; }
  { Pair p(Policy::coalesced,5s);p.connection->test_idle_reset();auto weak=std::weak_ptr(p.connection);p.connection->async_close();p.connection.reset();p.pump(100ms);
    require(weak.expired() && p.closed==1,"close must cancel self-held idle wait promptly");++count; }
  { Pair p(Policy::coalesced,5s);p.connection->test_idle_reset();p.connection->test_idle_payload();p.connection->async_close();p.pump(100ms);
    const auto waits=p.connection->test_idle_wait_count();p.connection->test_idle_response();p.connection->test_idle_reset();require(p.connection->test_idle_wait_count()==waits,"late response cannot resurrect a closed timer");++count; }
  { Pair p(Policy::coalesced,0ms);p.connection->test_idle_reset();p.pump(20ms);require(!p.closed && p.connection->test_idle_wait_count()==0,"zero timeout unchanged");++count; }
  { Pair p(Policy::coalesced);p.connection->test_idle_reset();p.connection->test_idle_payload();p.connection->test_idle_payload();p.connection->test_idle_response();
    p.pump(100ms);require(!p.closed,"outstanding second handler suppresses idle close");p.connection->test_idle_response();p.pump(500ms);require(p.closed==1,"last pipelined response starts idle deadline");++count; }
  for(int i=0;i<32;++i) { Pair p(Policy::coalesced,5s);p.connection->test_idle_reset();auto weak=std::weak_ptr(p.connection);p.connection->async_close();p.connection.reset();p.pump(100ms);require(weak.expired(),"connection churn must release cancelled timers"); }
  ++count;
  { Pair p(Policy::coalesced); p.connection->test_idle_reset();
    auto latest=p.connection->test_idle_deadline();
    asio::steady_timer refresh(p.io,10ms);
    refresh.async_wait([&](const auto &ec) { require(!ec,"refresh callback");p.connection->test_idle_reset();latest=p.connection->test_idle_deadline(); });
    p.pump(500ms);
    require(p.closed==1 && p.closed_at>=latest,"old expiry cannot close refreshed deadline early");
    require(p.connection->test_idle_wait_count()==2,"old expiry must rearm remaining deadline once");++count; }
  require(fds()==baseline,"FD restoration");std::cout<<"IDLE_TIMER_TESTS_PASS "<<count<<" FD_RESTORED\n";return 0;
} catch(const std::exception &e) {std::cerr<<"IDLE_TIMER_TEST_FAILURE "<<e.what()<<'\n';return 1;} }
