from pathlib import Path
import argparse, difflib, hashlib
parser = argparse.ArgumentParser(description="Generate the pinned native idle-monitor experiment; default RPC builds stay unchanged")
parser.add_argument('--upstream-include', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
p = args.output
source = args.upstream_include / 'ylt/coro_rpc/impl'
connection = (source/'coro_connection.hpp').read_text()
server = (source/'coro_rpc_server.hpp').read_text()
for name, text, expected in [('connection', connection, '18645f98989b185e86b70fe8db1d42136069bb817f2945927530d33b41920aca'), ('server', server, 'b5c19eeccebdbd44882ff2f615239daf4d013fe52d349abab3b619b93b2b59af')]:
 if hashlib.sha256(text.encode()).hexdigest() != expected:
  raise SystemExit('Pinned source mismatch: '+name)
p.mkdir(parents=True, exist_ok=False)
s=connection
assert hashlib.sha256(s.encode()).hexdigest()=='18645f98989b185e86b70fe8db1d42136069bb817f2945927530d33b41920aca'
def sub(a,b):
 global s
 assert s.count(a)==1,(a,s.count(a));s=s.replace(a,b)
sub('namespace coro_rpc {','namespace coro_rpc {\nenum class idle_timeout_policy { per_request, coalesced };')
sub('std::chrono::seconds(0))\n      : socket_wrapper_', 'std::chrono::seconds(0),\n                  idle_timeout_policy policy = idle_timeout_policy::per_request)\n      : socket_wrapper_')
sub('timer_(socket_wrapper_.get_executor()->get_asio_executor()) {','timer_(socket_wrapper_.get_executor()->get_asio_executor()),\n        idle_policy_(policy) {')
sub('    has_closed_ = true;\n    socket_wrapper_.close();','''    has_closed_ = true;
    if (idle_policy_ == idle_timeout_policy::coalesced) {
      idle_enabled_ = false;
      timer_.cancel();
    }
    socket_wrapper_.close();''')
sub('    timer_.expires_after(keep_alive_timeout_duration_);','''    if (idle_policy_ == idle_timeout_policy::coalesced) {
      if (has_closed_) return;
      idle_enabled_ = true;
      idle_deadline_ = std::chrono::steady_clock::now() + keep_alive_timeout_duration_;
      if (!idle_wait_pending_) arm_idle_monitor();
      return;
    }
#ifdef CORO_RPC_IDLE_TIMER_TEST
    ++idle_wait_count_;
#endif
    timer_.expires_after(keep_alive_timeout_duration_);''')
sub('    timer_.cancel();\n  }\n  coro_io::socket_wrapper_t', '''    if (idle_policy_ == idle_timeout_policy::coalesced) {
      idle_enabled_ = false;
      return;
    }
    timer_.cancel();
  }
  void arm_idle_monitor() {
    timer_.expires_at(idle_deadline_);
    timer_.async_wait([this, self = shared_from_this()](const asio::error_code &ec) {
      idle_wait_pending_ = false;
      if (ec || has_closed_ || !idle_enabled_ || rpc_processing_cnt_ != 0) return;
      if (std::chrono::steady_clock::now() < idle_deadline_) {
        arm_idle_monitor();
      } else {
        close();
      }
    });
    idle_wait_pending_ = true;
#ifdef CORO_RPC_IDLE_TIMER_TEST
    ++idle_wait_count_;
#endif
  }
  coro_io::socket_wrapper_t''')
sub('  asio::steady_timer timer_;','''  asio::steady_timer timer_;
  const idle_timeout_policy idle_policy_;
  std::chrono::steady_clock::time_point idle_deadline_{};
  bool idle_enabled_{false};
  bool idle_wait_pending_{false};
#ifdef CORO_RPC_IDLE_TIMER_TEST
  size_t idle_wait_count_{0};
#endif''')
sub('  using QuitCallback =', '''#ifdef CORO_RPC_IDLE_TIMER_TEST
  void test_idle_reset() { reset_timer(0); }
  void test_idle_payload() { cancel_timer(0); ++rpc_processing_cnt_; }
  void test_idle_response() { --rpc_processing_cnt_; reset_timer(0); }
  size_t test_idle_wait_count() const { return idle_wait_count_; }
  auto test_idle_deadline() const { return idle_deadline_; }
#endif
  using QuitCallback =''')
(p/'coro_connection-coalesced.hpp').write_text(s)
s=server
assert hashlib.sha256(s.encode()).hexdigest()=='b5c19eeccebdbd44882ff2f615239daf4d013fe52d349abab3b619b93b2b59af'
a='bool is_enable_tcp_no_delay = true)';assert s.count(a)==2
s=s.replace(a,'bool is_enable_tcp_no_delay = true,\n                       idle_timeout_policy policy = idle_timeout_policy::per_request)')
a='conn_timeout_duration_(conn_timeout_duration) {';assert s.count(a)==2
s=s.replace(a,'conn_timeout_duration_(conn_timeout_duration), idle_policy_(policy) {')
sub('          {})','          {},\n      idle_timeout_policy policy = idle_timeout_policy::per_request)')
sub('conn_timeout_duration_(config.conn_timeout_duration) {','conn_timeout_duration_(config.conn_timeout_duration), idle_policy_(policy) {')
sub('                                                    conn_timeout_duration_);','                                                    conn_timeout_duration_, idle_policy_);')
sub('  std::chrono::steady_clock::duration conn_timeout_duration_;','  std::chrono::steady_clock::duration conn_timeout_duration_;\n  const idle_timeout_policy idle_policy_;')
(p/'coro_rpc_server-coalesced.hpp').write_text(s)
print('OVERLAY_GENERATED')
