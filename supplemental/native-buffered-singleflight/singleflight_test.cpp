#include <ylt/coro_rpc/coro_rpc_client.hpp>
#include <asio/ip/address.hpp>
#include <dirent.h>
#include <poll.h>
#include <sys/socket.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <atomic>
#include <chrono>
#include <exception>
#include <iostream>
#include <optional>
#include <stdexcept>
#include <thread>
#include <vector>
using namespace std::chrono_literals;
using Client = coro_rpc::coro_rpc_client;
using Protocol = coro_rpc::protocol::coro_rpc_protocol;
using async_simple::coro::Lazy;
std::string_view Echo(std::string_view value) { return value; }
std::string EchoOwned(std::string_view value) { return std::string(value); }
void EchoVoid(std::string_view) {}
struct ReplyLike {
  uint32_t code;
  std::string message;
  std::string protobuf;
};
ReplyLike EchoLike(std::string_view value) {
  return {0, "ok", std::string(value)};
}
void require(bool ok, const char *text) {
  if (!ok)
    throw std::runtime_error(text);
}
struct Server {
  int listener = -1;
  int port = 0;
  std::thread thread;
  std::atomic<bool> stop{false};
  std::atomic<int> requests{0};
  std::string mode;
  std::string failure;
  explicit Server(std::string selected) : mode(std::move(selected)) {
    listener = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    require(listener >= 0, "socket");
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    require(bind(listener, (sockaddr *)&addr, sizeof(addr)) == 0, "bind");
    socklen_t n = sizeof(addr);
    require(getsockname(listener, (sockaddr *)&addr, &n) == 0, "getsockname");
    port = ntohs(addr.sin_port);
    require(listen(listener, 8) == 0, "listen");
    thread = std::thread([this] { serve(); });
  }
  ~Server() {
    stop = true;
    if (thread.joinable())
      thread.join();
    if (listener >= 0)
      ::close(listener);
  }
  bool wait(int fd, short events) {
    pollfd p{fd, events, 0};
    while (!stop) {
      int n = poll(&p, 1, 5);
      if (n > 0)
        return true;
      if (n < 0 && errno != EINTR)
        return false;
    }
    return false;
  }
  bool read(int fd, char *buf, size_t size) {
    while (size && !stop) {
      if (!wait(fd, POLLIN))
        return false;
      auto n = recv(fd, buf, size, MSG_DONTWAIT);
      if (n == 0)
        return false;
      if (n < 0) {
        if (errno == EAGAIN || errno == EINTR)
          continue;
        return false;
      }
      buf += n;
      size -= n;
    }
    return size == 0;
  }
  bool write(int fd, const char *buf, size_t size) {
    while (size && !stop) {
      if (!wait(fd, POLLOUT))
        return false;
      auto n = send(fd, buf, size, MSG_DONTWAIT | MSG_NOSIGNAL);
      if (n < 0) {
        if (errno == EAGAIN || errno == EINTR)
          continue;
        return false;
      }
      buf += n;
      size -= n;
    }
    return size == 0;
  }
  void pause(int ms) {
    for (int n = 0; n < ms && !stop; n++)
      std::this_thread::sleep_for(1ms);
  }
  void serve() noexcept {
    try {
      int accepted = 0;
      while (!stop) {
        if (!wait(listener, POLLIN))
          break;
        int fd =
            accept4(listener, nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC);
        if (fd < 0)
          continue;
        int connection = ++accepted;
        if (mode == "slowread" && connection == 1)
          pause(50);
        while (!stop) {
          char request[Protocol::REQ_HEAD_LEN];
          if (!read(fd, request, sizeof(request)))
            break;
          Protocol::req_header head{};
          auto e = struct_pack::deserialize_to<
              struct_pack::sp_config::DISABLE_ALL_META_INFO>(
              head, std::string_view(request, sizeof(request)));
          require(!e && head.magic == Protocol::magic_number,
                  "request header oracle");
          require(head.length <= 32 * 1024 * 1024 && head.attach_length <= 8192,
                  "request cap");
          std::string bytes(head.length, '\0'),
              attachment(head.attach_length, '\0');
          if (!read(fd, bytes.data(), bytes.size()) ||
              !read(fd, attachment.data(), attachment.size()))
            break;
          std::string payload;
          require(!struct_pack::deserialize_to(payload, bytes),
                  "request payload oracle");
          ++requests;
          if (mode == "delay")
            pause(40);
          auto encoded = struct_pack::serialize(std::string_view(payload));
          std::string body(encoded.data(), encoded.size());
          std::string reply_attachment = attachment;
          if (mode == "void") {
            auto v = struct_pack::serialize(std::monostate{});
            body.assign(v.data(), v.size());
          }
          if (mode == "structured") {
            auto v = struct_pack::serialize(ReplyLike{0, "ok", payload});
            body.assign(v.data(), v.size());
          }
          if (mode == "application_error" && requests == 1) {
            auto v = Protocol::prepare_response(
                body, head, attachment.size(),
                static_cast<coro_rpc::errc>(10000), "application error");
            if (!write(fd, v.data(), v.size()) ||
                !write(fd, body.data(), body.size()) ||
                !write(fd, attachment.data(), attachment.size()))
              break;
            continue;
          }
          Protocol::resp_header reply{};
          reply.magic = Protocol::magic_number;
          reply.version = Protocol::VERSION_NUMBER;
          reply.seq_num = head.seq_num;
          reply.length = body.size();
          reply.attach_length = reply_attachment.size();
          if (mode == "magic" || (mode == "cached_bad_once" && connection == 1))
            ++reply.magic;
          if (mode == "version")
            ++reply.version;
          if (mode == "sequence")
            ++reply.seq_num;
          if (mode == "type")
            reply.msg_type = 1;
          if (mode == "body_limit")
            reply.length = 8193;
          if (mode == "attachment_limit")
            reply.attach_length = 4097;
          if (mode == "bad_body") {
            body = "x";
            reply.length = 1;
          }
          if (mode == "empty_body") {
            body.clear();
            reply.length = 0;
          }
          if (mode == "remote_error") {
            auto error = struct_pack::serialize(std::string("remote failure"));
            body.assign(error.data(), error.size());
            reply.length = body.size();
            reply.err_code = 1;
          }
          auto header = struct_pack::serialize<
              struct_pack::sp_config::DISABLE_ALL_META_INFO>(reply);
          require(header.size() == Protocol::RESP_HEAD_LEN,
                  "response header size");
          if (mode == "short_header") {
            write(fd, header.data(), 5);
            break;
          }
          if (mode == "partial_header") {
            write(fd, header.data(), 5);
            pause(50);
            break;
          }
          if (mode == "cached_bad_once" && connection == 1) {
            std::string all(header.data(), header.size());
            all += body;
            all += reply_attachment;
            if (!write(fd, all.data(), all.size()))
              break;
            continue;
          }
          if (mode == "fragmented") {
            std::string all(header.data(), header.size());
            all += body;
            all += reply_attachment;
            bool ok = true;
            for (size_t off = 0; off < all.size();) {
              size_t n = std::min<size_t>(17, all.size() - off);
              if (!write(fd, all.data() + off, n)) {
                ok = false;
                break;
              }
              off += n;
            }
            if (!ok)
              break;
            continue;
          }
          if (!write(fd, header.data(), header.size()))
            break;
          if (mode == "body_limit" || mode == "attachment_limit") {
            pause(50);
            break;
          }
          if (mode == "partial_body") {
            write(fd, body.data(), 2);
            pause(50);
            break;
          }
          if (mode == "short_body") {
            write(fd, body.data(), body.size() - 1);
            break;
          }
          if (!write(fd, body.data(), body.size()))
            break;
          if (mode == "short_attachment") {
            write(fd, reply_attachment.data(), 1);
            break;
          }
          if (!write(fd, reply_attachment.data(), reply_attachment.size()))
            break;
          if (mode == "fin")
            break;
        }
        ::close(fd);
      }
    } catch (const std::exception &e) {
      failure = e.what();
    }
  }
};
template <class T> struct State {
  bool done = false;
  std::optional<T> result;
  std::exception_ptr error;
};
struct Owner {
  asio::io_context context;
  coro_io::ExecutorWrapper<> executor{context.get_executor()};
  std::unique_ptr<Client> client;
  int active = 0;
  bool deadline = false;
  explicit Owner(bool single, bool bounded = true, size_t bodycap = 8192,
                 size_t attcap = 4096) {
    Client::config conf;
    conf.max_response_body_bytes = bounded ? bodycap : 0;
    conf.max_response_attachment_bytes = bounded ? attcap : 0;
    if (single)
      client =
          std::make_unique<Client>(&executor, conf, Client::singleflight_t{});
    else
      client = std::make_unique<Client>(&executor, conf);
  }
  ~Owner() {
    try {
      client->close();
      auto limit = std::chrono::steady_clock::now() + 2s;
      context.restart();
      while ((active || !context.stopped()) &&
             std::chrono::steady_clock::now() < limit)
        context.run_one_for(5ms);
      if (active || !context.stopped())
        std::_Exit(90);
      client.reset();
      context.restart();
      while (context.poll_one()) {
      }
    } catch (...) {
      std::_Exit(91);
    }
  }
  template <class T> std::shared_ptr<State<T>> start(Lazy<T> task) {
    auto state = std::make_shared<State<T>>();
    if (context.stopped())
      context.restart();
    ++active;
    std::move(task).via(&executor).start(
        [this, state](async_simple::Try<T> &&value) {
          require(executor.currentThreadInExecutor(), "owner executor");
          if (value.hasError())
            state->error = value.getException();
          else
            state->result.emplace(std::move(value).value());
          --active;
          state->done = true;
        });
    return state;
  }
  template <class T>
  T finish(std::shared_ptr<State<T>> state,
           std::chrono::milliseconds budget = 500ms) {
    auto end = std::chrono::steady_clock::now() + budget;
    while (!state->done && std::chrono::steady_clock::now() < end)
      context.run_one_until(end);
    if (!state->done) {
      deadline = true;
      client->close();
      end = std::chrono::steady_clock::now() + 2s;
      while (!state->done && std::chrono::steady_clock::now() < end)
        context.run_one_until(end);
      require(state->done, "cancellation failed to drain");
    }
    if (state->error)
      std::rethrow_exception(state->error);
    return std::move(*state->result);
  }
  template <class T>
  T run(Lazy<T> task, std::chrono::milliseconds budget = 500ms) {
    return finish(start(std::move(task)), budget);
  }
  void connect(int port) {
    std::vector<asio::ip::tcp::endpoint> addresses{
        {asio::ip::make_address("127.0.0.1"),
         static_cast<unsigned short>(port)}};
    auto result = run(
        client->connect("127.0.0.1", std::to_string(port), 200ms, &addresses));
    require(!result, "connect failed");
  }
  coro_rpc::request_config_t config() {
    coro_rpc::request_config_t value{};
    value.request_timeout_duration = -1ms;
    return value;
  }
  template <auto Fn>
  Lazy<coro_rpc::async_rpc_result<decltype(coro_rpc::get_return_type<Fn>())>>
  typed(coro_rpc::request_config_t conf, std::string_view data) {
    co_return co_await client->call_singleflight<Fn>(conf, data);
  }
  Lazy<coro_rpc::async_rpc_result<std::string_view>>
  pipeline(coro_rpc::request_config_t conf, std::string_view data) {
    auto pending = co_await client->send_request<&Echo>(conf, data);
    co_return co_await std::move(pending);
  }
  Lazy<coro_rpc::async_rpc_result<std::string_view>>
  single(coro_rpc::request_config_t conf, std::string_view data) {
    co_return co_await client->call_singleflight<&Echo>(conf, data);
  }
};
int fds() {
  int count = 0;
  DIR *dir = opendir("/proc/self/fd");
  require(dir, "fd directory");
  while (auto e = readdir(dir))
    if (e->d_name[0] != '.')
      ++count;
  closedir(dir);
  return count;
}
int main() {
  try {
    {
      Server server("ok");
      Owner owner(false);
      owner.connect(server.port);
      auto result = owner.run(owner.pipeline(owner.config(), "initialize"));
      require(bool(result), "pipeline initialization");
    }
    const int baseline = fds();
    int tests = 0;
    std::string payload(4096, 'p');
    {
      Server server("ok");
      Owner owner(true);
      owner.connect(server.port);
      auto conf = owner.config();
      std::string attachment(1024, 'a');
      conf.request_attachment = attachment;
      std::optional<coro_rpc::async_rpc_result<std::string_view>> retained;
      for (int i = 0; i < 3; i++) {
        auto result = owner.run(owner.single(conf, payload));
        require(result && result->result() == payload &&
                    result->get_attachment() == attachment,
                "reused bytes and attachment");
        retained.emplace(std::move(result));
      }
      owner.client->close();
      require((*retained)->result() == payload &&
                  (*retained)->get_attachment() == attachment,
              "retained views after close");
      ++tests;
    }
    for (bool single : {false, true}) {
      Server server("ok");
      Owner owner(single);
      owner.connect(server.port);
      auto rejected = single ? owner.run(owner.pipeline(owner.config(), "mode"))
                             : owner.run(owner.single(owner.config(), "mode"));
      require(!rejected && server.requests == 0, "mode isolation before write");
      auto accepted = single
                          ? owner.run(owner.single(owner.config(), "mode"))
                          : owner.run(owner.pipeline(owner.config(), "mode"));
      require(accepted && accepted->result() == "mode",
              "mode valid after rejection");
      ++tests;
    }
    {
      Server server("ok");
      Owner owner(true);
      owner.connect(server.port);
      auto conf = owner.config();
      conf.request_timeout_duration.reset();
      require(!owner.run(owner.single(conf, "timeout")),
              "missing external mode");
      conf.request_timeout_duration = 100ms;
      require(!owner.run(owner.single(conf, "timeout")), "timed request mode");
      require(server.requests == 0, "timed rejection before write");
      ++tests;
    }
    {
      Server server("ok");
      Owner owner(true, false);
      owner.connect(server.port);
      require(!owner.run(owner.single(owner.config(), "limits")) &&
                  server.requests == 0,
              "bounds required before write");
      ++tests;
    }
    for (const char *mode :
         {"magic", "version", "sequence", "type", "body_limit",
          "attachment_limit", "short_header", "short_body", "short_attachment",
          "bad_body", "empty_body", "remote_error"}) {
      Server server(mode);
      Owner owner(true);
      owner.connect(server.port);
      auto conf = owner.config();
      std::string attachment(16, 'b');
      conf.request_attachment = attachment;
      auto result = owner.run(owner.single(conf, payload));
      require(!result && !owner.deadline, "malformed response rejection");
      require(owner.client->has_closed(), "malformed connection retired");
      ++tests;
    }
    {
      Server server("delay");
      Owner owner(true);
      owner.connect(server.port);
      auto first = owner.start(owner.single(owner.config(), "first"));
      auto second = owner.start(owner.single(owner.config(), "second"));
      auto b = owner.finish(second);
      auto a = owner.finish(first);
      require(a && !b && server.requests == 1, "single flight admission");
      require(owner.run(owner.single(owner.config(), "third")) &&
                  server.requests == 2,
              "reuse after overlap rejection");
      ++tests;
    }
    for (const char *mode : {"partial_body", "slowread"}) {
      Server server(mode);
      Owner owner(true);
      owner.connect(server.port);
      std::string input = std::string(mode) == "slowread"
                              ? std::string(16 * 1024 * 1024, 'z')
                              : payload;
      auto result = owner.run(owner.single(owner.config(), input), 3ms);
      require(!result && owner.deadline && owner.active == 0,
              "external read or write deadline and drain");
      ++tests;
    }
    {
      Server server("fin");
      Owner owner(true);
      owner.connect(server.port);
      require(bool(owner.run(owner.single(owner.config(), "first"))),
              "first FIN response");
      require(!owner.run(owner.single(owner.config(), "stale")),
              "stale FIN no retry");
      owner.connect(server.port);
      require(bool(owner.run(owner.single(owner.config(), "new"))),
              "reconnect after FIN");
      ++tests;
    }
    {
      Server server("delay");
      using R = coro_rpc::async_rpc_result<std::string_view>;
      std::shared_ptr<State<R>> state;
      {
        Owner owner(true);
        owner.connect(server.port);
        state = owner.start(owner.single(owner.config(), "destroy"));
        auto until = std::chrono::steady_clock::now() + 100ms;
        while (server.requests == 0 && std::chrono::steady_clock::now() < until)
          owner.context.run_one_for(1ms);
        require(server.requests == 1 && !state->done,
                "destruction with an active read");
      }
      require(state->done && state->result && !*state->result,
              "owner destruction closes and drains");
      ++tests;
    }
    {
      Server server("ok");
      std::optional<coro_rpc::async_rpc_result<std::string_view>> retained;
      {
        Owner owner(true);
        owner.connect(server.port);
        retained.emplace(owner.run(owner.single(owner.config(), "tiny")));
      }
      require(retained && *retained && (*retained)->result() == "tiny",
              "tiny view remains valid after owner destruction");
      ++tests;
    }
    for (int external_size : {2, 4096}) {
      Server server("ok");
      std::vector<char> external(external_size);
      std::string attachment(16, 'f');
      std::optional<coro_rpc::async_rpc_result<std::string_view>> retained;
      {
        Owner owner(true);
        owner.connect(server.port);
        auto conf = owner.config();
        conf.request_attachment = attachment;
        conf.resp_attachment_buf = external;
        retained.emplace(owner.run(owner.single(conf, "small")));
        require(*retained && ((*retained)->get_attachment().data() ==
                              external.data()) == (external_size >= 16),
                "external attachment or owned fallback");
      }
      require((*retained)->result() == "small" &&
                  (*retained)->get_attachment() == attachment,
              "small response and attachment after owner destruction");
      ++tests;
    }
    {
      Server server("ok");
      Owner owner(true);
      owner.connect(server.port);
      auto conf = owner.config();
      std::string attachment(4096, 'b');
      conf.request_attachment = attachment;
      auto value = owner.run(owner.single(conf, "boundary"));
      require(value && value->get_attachment() == attachment,
              "attachment limit boundary");
      ++tests;
    }
    {
      Server server("ok");
      Owner owner(true);
      owner.connect(server.port);
      auto value = owner.run(owner.typed<&EchoOwned>(owner.config(), payload));
      require(value && value->result() == payload, "owning typed reply");
      ++tests;
    }
    {
      Server server("void");
      Owner owner(true);
      owner.connect(server.port);
      auto value = owner.run(owner.typed<&EchoVoid>(owner.config(), payload));
      require(bool(value), "void reply");
      ++tests;
    }
    {
      Server server("structured");
      Owner owner(true);
      owner.connect(server.port);
      auto value = owner.run(owner.typed<&EchoLike>(owner.config(), payload));
      require(value && value->result().code == 0 &&
                  value->result().message == "ok" &&
                  value->result().protobuf == payload,
              "structured reply");
      ++tests;
    }
    {
      Server server("application_error");
      Owner owner(true);
      owner.connect(server.port);
      auto error = owner.run(owner.single(owner.config(), "first"));
      require(!error && !owner.client->has_closed(),
              "application error preserves connection");
      auto value = owner.run(owner.single(owner.config(), "next"));
      require(value && value->result() == "next",
              "reuse after application error");
      ++tests;
    }
    {
      Server server("delay");
      Owner owner(true);
      owner.connect(server.port);
      auto first = owner.start(owner.single(owner.config(), "active"));
      auto until = std::chrono::steady_clock::now() + 200ms;
      while (server.requests == 0 && std::chrono::steady_clock::now() < until)
        owner.context.run_one_for(1ms);
      require(server.requests == 1 && !first->done, "active read established");
      std::vector<asio::ip::tcp::endpoint> eps{
          {asio::ip::make_address("127.0.0.1"),
           static_cast<unsigned short>(server.port)}};
      auto reconnect = owner.run(owner.client->connect(
          "127.0.0.1", std::to_string(server.port), 200ms, &eps));
      require(bool(reconnect),
              "connect during active singleflight must refuse");
      auto value = owner.finish(first);
      require(value && value->result() == "active",
              "refused connect preserves active read");
      ++tests;
    }
    for (size_t size : {size_t(1), size_t(8200), size_t(1024 * 1024)}) {
      Server server("ok");
      Owner owner(true, true, 2 * 1024 * 1024, 8192);
      owner.connect(server.port);
      std::string large(size, 'L'), att(4096, 'A');
      auto conf = owner.config();
      conf.request_attachment = att;
      auto retained = owner.run(owner.single(conf, large));
      require(retained && retained->result() == large &&
                  retained->get_attachment() == att,
              "cache prefix and raw bulk exact bytes");
      for (auto small : {"", "next", "tiny"}) {
        auto value = owner.run(owner.single(owner.config(), small));
        require(value && value->result() == small, "large to small reuse");
      }
      owner.client->close();
      require(retained->result() == large && retained->get_attachment() == att,
              "retained large views after cache reuse");
      ++tests;
    }
    {
      Server server("fragmented");
      Owner owner(true);
      owner.connect(server.port);
      auto conf = owner.config();
      std::string att(4096, 'F');
      conf.request_attachment = att;
      auto value = owner.run(owner.single(conf, payload));
      require(value && value->result() == payload &&
                  value->get_attachment() == att,
              "fragmented header body and attachment");
      ++tests;
    }
    {
      Server server("cached_bad_once");
      Owner owner(true);
      owner.connect(server.port);
      auto value = owner.run(owner.single(owner.config(), "badcached"));
      require(!value && owner.client->has_closed(),
              "bad coalesced response closes");
#ifdef BUFFERED_CACHE_TEST
      require(owner.client->singleflight_cached_bytes() > 0,
              "regression fixture has actual unread cache");
#endif
      owner.connect(server.port);
#ifdef BUFFERED_CACHE_TEST
      require(owner.client->singleflight_cached_bytes() == 0,
              "reset discards old cache before replacing socket");
#endif
      auto next = owner.run(owner.single(owner.config(), "fresh"));
      require(next && next->result() == "fresh",
              "fresh reply after cached protocol error");
      ++tests;
    }
    {
      Server server("ok");
      Owner owner(true, true, 32, 1);
      owner.connect(server.port);
      auto value = owner.run(owner.single(owner.config(), ""));
      require(value && value->result().empty(),
              "empty value under tiny positive caps");
      ++tests;
    }
    {
      Server server("partial_header");
      Owner owner(true);
      owner.connect(server.port);
      auto result = owner.run(owner.single(owner.config(), "partial"), 3ms);
      require(!result && owner.deadline && owner.active == 0,
              "external deadline cancels and drains partial cache fill");
      ++tests;
    }
    {
      Server server("ok");
      Owner owner(true);
      std::vector<asio::ip::tcp::endpoint> eps{
          {asio::ip::make_address("127.0.0.1"),
           static_cast<unsigned short>(server.port)}};
      auto first = owner.start(owner.client->connect(
          "127.0.0.1", std::to_string(server.port), 200ms, &eps));
      auto second = owner.start(owner.client->connect(
          "127.0.0.1", std::to_string(server.port), 200ms, &eps));
      auto b = owner.finish(second);
      auto a = owner.finish(first);
      require(!a && b, "concurrent connects admitted only once");
      require(bool(owner.run(owner.single(owner.config(), "connected"))),
              "accepted connect remains usable");
      ++tests;
    }
    require(fds() == baseline, "FD restoration");
    std::cout << "SINGLEFLIGHT_TESTS_PASS " << tests << " FD_RESTORED\n";
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "SINGLEFLIGHT_TEST_FAILURE " << error.what() << '\n';
    return 1;
  }
}
