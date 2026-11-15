"""Generate a bounded-cache singleflight overlay without modifying input headers."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
MODULE = Path(__file__).resolve().parent
REPO = MODULE.parents[1]
BASE_SHA = 'c616782568336a0398640b80eed3fd501a72291098c95e468dd3a5730a16e923'

def transform(s):
    assert hashlib.sha256(s.encode()).hexdigest() == '078c513d471146e316db926cb9d1435972eb1cf1792409437af61d43673d1b58'
    s = '#include <asio/buffered_read_stream.hpp>\n' + s
    needle = '    coro_io::socket_wrapper_t socket_wrapper_;'
    assert s.count(needle) == 1
    s = s.replace(needle, needle + '\n    std::unique_ptr<asio::buffered_read_stream<asio::ip::tcp::socket &>> singleflight_reader_;\n    bool singleflight_connecting_ = false;')
    needle = '    auto lock_ok = connect_mutex_.tryLock();'
    assert s.count(needle) == 1
    s = s.replace(needle, '    if (singleflight_only_ && (!control_->executor_->currentThreadInExecutor() ||\n        control_->recving_cnt_.load(std::memory_order_acquire) != 0 ||\n        control_->singleflight_connecting_)) {\n      co_return errc::not_connected;\n    }\n    struct connect_guard {\n      bool *value;\n      ~connect_guard() { if (value) *value = false; }\n    } single_connect{singleflight_only_ ? &control_->singleflight_connecting_ : nullptr};\n    if (single_connect.value) *single_connect.value = true;\n' + needle)
    needle = '    co_await close_socket(control_);\n    bool reset_ok'
    assert s.count(needle) == 1
    s = s.replace(needle, '    co_await close_socket(control_);\n    control_->singleflight_reader_.reset();\n    bool reset_ok')
    needle = '    auto ret = co_await coro_io::async_read(socket, asio::buffer(data));'
    assert s.count(needle) == 1
    s = s.replace(needle, '    if (!controller->singleflight_reader_) {\n      const size_t cache_size = std::min<uint64_t>(8192, sizeof(data) +\n          std::min<uint64_t>(8192, controller->max_response_body_bytes) +\n          std::min<uint64_t>(8192, controller->max_response_attachment_bytes));\n      controller->singleflight_reader_ =\n          std::make_unique<asio::buffered_read_stream<asio::ip::tcp::socket &>>(socket, cache_size);\n    }\n    auto &reader = *controller->singleflight_reader_;\n    auto ret = co_await coro_io::async_read(reader, asio::buffer(data));')
    a = s.index('    if (header.attach_length == 0) {', s.index('read_single_response('))
    b = s.index('    if (ret.first) co_return async_rpc_raw_result{ret.first};', a)
    s = s[:a] + '    if (header.attach_length == 0) {\n      attachment = {};\n    } else if (attachment.size() < header.attach_length) {\n      auto *buffer = response.resp_attachment_buf_.get_string();\n      if (!buffer) co_return async_rpc_raw_result{std::make_error_code(std::errc::operation_not_supported)};\n      struct_pack::detail::resize(*buffer,\n          std::max<uint32_t>(header.attach_length, sizeof(std::string)));\n      attachment = {std::span<char>{buffer->data(), header.attach_length}, -1};\n    } else {\n      attachment = {attachment.substr(0, header.attach_length), -1};\n    }\n    size_t body_copied = 0, attachment_copied = 0;\n    if (header.length != 0 && reader.in_avail() != 0) {\n      body_copied = reader.read_some(\n          asio::buffer(response.read_buf_.data(), header.length), ret.first);\n      if (ret.first) co_return async_rpc_raw_result{ret.first};\n    }\n    if (header.attach_length != 0 && reader.in_avail() != 0) {\n      attachment_copied = reader.read_some(\n          asio::buffer(attachment.mutable_data(), header.attach_length), ret.first);\n      if (ret.first) co_return async_rpc_raw_result{ret.first};\n    }\n    if (body_copied < header.length || attachment_copied < header.attach_length) {\n      if (reader.in_avail() != 0)\n        co_return async_rpc_raw_result{std::make_error_code(std::errc::protocol_error)};\n      std::array<asio::mutable_buffer, 2> buffers{\n          asio::buffer(response.read_buf_.data() + body_copied, header.length - body_copied),\n          attachment_copied < header.attach_length\n              ? asio::buffer(attachment.mutable_data() + attachment_copied,\n                             header.attach_length - attachment_copied)\n              : asio::mutable_buffer{}};\n      ret = co_await coro_io::async_read(socket, buffers);\n    }\n' + s[b:]
    needle = '    if (controller->has_closed_) {'
    a = s.index(needle, s.index('  call_singleflight('))
    s = s[:a] + s[a:].replace(needle, '    if (controller->has_closed_ || controller->singleflight_connecting_) {', 1)
    a = s.index('      auto result = co_await controller->socket_wrapper_.visit(', s.index('  call_singleflight('))
    b = s.index('    } catch (...) {', a)
    s = s[:a] + '      auto *socket = controller->socket_wrapper_.visit([](auto &active) -> asio::ip::tcp::socket * {\n        if constexpr (std::is_same_v<std::remove_cvref_t<decltype(active)>, asio::ip::tcp::socket>)\n          return &active;\n        else\n          return nullptr;\n      });\n      if (!socket) {\n        co_return unexpected<rpc_error>{rpc_error{errc::invalid_rpc_result, "singleflight supports plain TCP only"}};\n      }\n      auto id = sequence;\n      auto error = co_await send_impl<func>(*socket, id,\n          coro_io::data_view{config.request_attachment, config.request_attachment_gpu_id},\n          std::forward<Args>(args)...);\n      if (error) {\n        close_socket_async(controller);\n        co_return unexpected<rpc_error>{std::move(error)};\n      }\n      auto raw = co_await read_single_response(controller, *socket, sequence, config);\n      if (raw.index() == 1) close_socket_async(controller);\n      co_return decode_single_response<return_t>(std::move(raw), controller, controller->client_id);\n' + s[b:]
    needle = '#ifdef UNIT_TEST_INJECT\n public:\n'
    assert s.count(needle) == 1
    s = s.replace(needle, '#ifdef CORO_RPC_SINGLEFLIGHT_TEST\n public:\n  size_t singleflight_cached_bytes() const {\n    return control_->singleflight_reader_ ? control_->singleflight_reader_->in_avail() : 0;\n  }\n#endif\n' + needle)
    return s

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--header', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--omit-reset', action='store_true', help='Deliberate mutation for the cache-reset regression test only')
    args = parser.parse_args()
    if not args.header.is_file() or hashlib.sha256(args.header.read_bytes()).hexdigest() != BASE_SHA:
        parser.error('Exact prepared YLT base header required; input preserved')
    if args.output_dir.exists():
        parser.error('Output directory must be new')
    args.output_dir.mkdir(parents=True)
    subprocess.run([sys.executable, str(REPO / 'supplemental/native-singleflight/apply_overlay.py'),
                    '--header', str(args.header), '--output-dir', str(args.output_dir / 'singleflight-v1')], check=True)
    source = transform((args.output_dir / 'singleflight-v1/coro_rpc_client-single.hpp').read_text())
    if args.omit_reset:
        source = source.replace('    control_->singleflight_reader_.reset();\n', '')
    (args.output_dir / 'coro_rpc_client-single.hpp').write_text(source)
    print('buffered singleflight overlay', hashlib.sha256(source.encode()).hexdigest())

if __name__ == '__main__':
    main()
