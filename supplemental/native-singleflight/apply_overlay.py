from pathlib import Path
import hashlib,difflib
import argparse
parser=argparse.ArgumentParser(description='Generate an opt-in singleflight experiment from the exact patched DataSystem YLT header.')
parser.add_argument('--header',type=Path,required=True)
parser.add_argument('--output-dir',type=Path,required=True)
args=parser.parse_args()
base=args.header.read_text()
if hashlib.sha256(base.encode()).hexdigest()!='c616782568336a0398640b80eed3fd501a72291098c95e468dd3a5730a16e923':
    raise SystemExit('Input header differs from the frozen patched DataSystem base; refusing to guess.')
r=args.output_dir
r.mkdir(parents=True,exist_ok=False)
s=base
start=s.index('  coro_rpc_client(\n');end=s.index('  coro_rpc_client(const std::string &local_ip)',start);original=s[start:end]
private=original.replace('      coro_io::ExecutorWrapper<> *executor = coro_io::get_global_executor(),\n      config conf = {})','      coro_io::ExecutorWrapper<> *executor, config conf, bool singleflight_only)')
private=private.replace(': timer_(',': singleflight_only_(singleflight_only), timer_(')
s=s[:start]+'''  struct singleflight_t {};
  coro_rpc_client(coro_io::ExecutorWrapper<> *executor = coro_io::get_global_executor(),
                  config conf = {})
      : coro_rpc_client(executor, std::move(conf), false) {}
  coro_rpc_client(coro_io::ExecutorWrapper<> *executor, config conf, singleflight_t)
      : coro_rpc_client(executor, std::move(conf), true) {}

 private:
'''+private+' public:\n'+s[end:]
s=s.replace('  std::unique_ptr<coro_io::period_timer> timer_;\n  std::shared_ptr<control_t> control_;','  const bool singleflight_only_;\n  std::unique_ptr<coro_io::period_timer> timer_;\n  std::shared_ptr<control_t> control_;')
assert s.count('const bool singleflight_only_;')==1
# Keep the existing pipeline decoder unchanged; the prototype reuses its exact typed decoding body.
a=base.index('    if (ret_.index() == 1)',base.index('static async_simple::coro::Lazy<async_rpc_result<T>> deserialize_rpc_result('));b=base.index('\n public:',a);decode=base[a:b];assert decode.endswith('  }\n')
decode=decode.replace('co_return','return')
new='''  template <typename T>
  static async_rpc_result<T> decode_single_response(
      async_rpc_raw_result ret_, std::weak_ptr<control_t> watcher, uint64_t client_id) {
'''+decode+'''
  struct singleflight_guard {
    std::shared_ptr<control_t> control;
    explicit singleflight_guard(std::shared_ptr<control_t> value) : control(std::move(value)) {}
    singleflight_guard(const singleflight_guard &) = delete;
    ~singleflight_guard() { control->recving_cnt_.fetch_sub(1, std::memory_order_release); }
  };

  static async_simple::coro::Lazy<async_rpc_raw_result> read_single_response(
      std::shared_ptr<control_t> controller, asio::ip::tcp::socket &socket,
      uint32_t sequence, request_config_t &config) {
    coro_rpc_protocol::resp_header header{};
    char data[coro_rpc_protocol::RESP_HEAD_LEN];
    auto ret = co_await coro_io::async_read(socket, asio::buffer(data));
    if (ret.first) co_return async_rpc_raw_result{ret.first};
    auto ec = struct_pack::deserialize_to<struct_pack::sp_config::DISABLE_ALL_META_INFO>(
        header, std::string_view(data, sizeof(data)));
    if (ret.second != sizeof(data) || ec || header.magic != coro_rpc_protocol::magic_number ||
        header.version > coro_rpc_protocol::VERSION_NUMBER || header.msg_type != 0 ||
        header.seq_num != sequence) {
      co_return async_rpc_raw_result{std::make_error_code(std::errc::protocol_error)};
    }
    if (response_exceeds_limit(header.length, controller->max_response_body_bytes) ||
        response_exceeds_limit(header.attach_length, controller->max_response_attachment_bytes)) {
      co_return async_rpc_raw_result{std::make_error_code(std::errc::message_size)};
    }
    resp_body response;
    struct_pack::detail::resize(response.read_buf_,
        std::max<uint32_t>(header.length, sizeof(std::string)));
    if (header.length < sizeof(std::string)) response.read_buf_.resize(header.length);
    coro_io::data_view attachment{config.resp_attachment_buf, config.resp_attachment_buf_gpu_id};
    if (header.attach_length == 0) {
      attachment = {};
      ret = co_await coro_io::async_read(socket,
          asio::buffer(response.read_buf_.data(), header.length));
    } else {
      if (attachment.size() < header.attach_length) {
        auto *buffer = response.resp_attachment_buf_.get_string();
        if (!buffer) co_return async_rpc_raw_result{std::make_error_code(std::errc::operation_not_supported)};
        struct_pack::detail::resize(*buffer,
            std::max<uint32_t>(header.attach_length, sizeof(std::string)));
        attachment = {std::span<char>{buffer->data(), header.attach_length}, -1};
      } else {
        attachment = {attachment.substr(0, header.attach_length), -1};
      }
      std::array<asio::mutable_buffer, 2> buffers{
          asio::buffer(response.read_buf_.data(), header.length),
          asio::buffer(attachment.mutable_data(), header.attach_length)};
      ret = co_await coro_io::async_read(socket, buffers);
    }
    if (ret.first) co_return async_rpc_raw_result{ret.first};
    co_return async_rpc_raw_result{async_rpc_raw_result_value_type{
        std::move(response), attachment, header.err_code}};
  }

 public:
  template <auto func, typename... Args>
  async_simple::coro::Lazy<async_rpc_result<decltype(get_return_type<func>())>>
  call_singleflight(request_config_t config, Args &&...args) {
    using return_t = decltype(get_return_type<func>());
    static_check<func, Args...>();
    auto controller = control_;
    if (!singleflight_only_ || !config.request_timeout_duration ||
        config.request_timeout_duration->count() >= 0 ||
        !std::holds_alternative<tcp_config>(config_.socket_config) ||
        config.request_attachment_gpu_id >= 0 || config.resp_attachment_buf_gpu_id >= 0 ||
        controller->max_response_body_bytes == 0 || controller->max_response_attachment_bytes == 0 ||
        !controller->executor_->currentThreadInExecutor()) {
      co_return unexpected<rpc_error>{rpc_error{errc::invalid_rpc_result, "singleflight mode requires a bounded exclusive TCP owner and external deadline"}};
    }
    if (controller->has_closed_) {
      co_return unexpected<rpc_error>{rpc_error{errc::io_error, "singleflight connection is closed"}};
    }
    uint32_t expected = 0;
    if (!controller->recving_cnt_.compare_exchange_strong(expected, 1, std::memory_order_acq_rel)) {
      co_return unexpected<rpc_error>{rpc_error{errc::invalid_rpc_result, "singleflight call already active"}};
    }
    singleflight_guard guard(controller);
    const auto sequence = request_id_++;
    try {
      auto result = co_await controller->socket_wrapper_.visit(
          [this, controller, sequence, &config, &args...](auto &socket)
          -> async_simple::coro::Lazy<async_rpc_result<return_t>> {
        if constexpr (std::is_same_v<std::remove_cvref_t<decltype(socket)>, asio::ip::tcp::socket>) {
          auto id = sequence;
          auto error = co_await send_impl<func>(socket, id, coro_io::data_view{config.request_attachment, config.request_attachment_gpu_id},
                                               std::forward<Args>(args)...);
          if (error) {
            close_socket_async(controller);
            co_return unexpected<rpc_error>{std::move(error)};
          }
          auto raw = co_await read_single_response(controller, socket, sequence, config);
          if (raw.index() == 1) close_socket_async(controller);
          co_return decode_single_response<return_t>(std::move(raw), controller, controller->client_id);
        } else {
          co_return unexpected<rpc_error>{rpc_error{errc::invalid_rpc_result, "singleflight supports plain TCP only"}};
        }
      });
      co_return std::move(result);
    } catch (...) {
      close_socket_async(controller);
      throw;
    }
  }

 private:
'''
# Insert before the unchanged public pipeline API, after all private helper types are declared.
s=s[:s.index('\n public:',s.index('static async_simple::coro::Lazy<async_rpc_result<T>> deserialize_rpc_result('))]+ '\n'+new+s[s.index('\n public:',s.index('static async_simple::coro::Lazy<async_rpc_result<T>> deserialize_rpc_result(')):]
needle='''    using rpc_return_t = decltype(get_return_type<func>());
    recving_guard guard(control_.get());'''
assert s.count(needle)==1
s=s.replace(needle,'''    using rpc_return_t = decltype(get_return_type<func>());
    if (singleflight_only_) {
      co_return build_failed_rpc_result<rpc_return_t>(rpc_error{
          errc::invalid_rpc_result, "pipeline API disabled on singleflight client"});
    }
    recving_guard guard(control_.get());''')
(r/'coro_rpc_client-single.hpp').write_text(s)
patch=''.join(difflib.unified_diff(base.splitlines(True),s.splitlines(True),fromfile='a/include/ylt/coro_rpc/impl/coro_rpc_client.hpp',tofile='b/include/ylt/coro_rpc/impl/coro_rpc_client.hpp'))
(r/'singleflight-experiment.patch').write_text(patch)
print('experimental source overlay',hashlib.sha256(s.encode()).hexdigest(),'patchlines',len(patch.splitlines()))
