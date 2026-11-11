#include <ylt/coro_io/coro_io.hpp>

#include <algorithm>
#include <array>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
void require(bool value, const char* message)
{
    if (!value) throw std::runtime_error(message);
}

class Stream {
public:
    using executor_type = asio::io_context::executor_type;
    explicit Stream(asio::io_context& io) : io_(io) {}
    executor_type get_executor() { return io_.get_executor(); }
    void cancel() {}

    template <typename Buffers, typename Handler>
    void async_write_some(const Buffers& buffers, Handler&& handler)
    {
        const auto offered = asio::buffer_size(buffers);
        offers.push_back(offered);
        auto bytes = std::min(offered, partialLimit);
        std::error_code error;
        if (offers.size() == errorOnCall) {
            error = asio::error::broken_pipe;
            bytes = std::min(bytes, bytesOnError);
        }
        std::string copied(bytes, '\0');
        asio::buffer_copy(asio::buffer(copied), buffers, bytes);
        output += copied;
        asio::post(io_, [handler = std::forward<Handler>(handler), error, bytes]() mutable {
            handler(error, bytes);
        });
    }

    std::size_t partialLimit = std::numeric_limits<std::size_t>::max();
    std::size_t errorOnCall = 0;
    std::size_t bytesOnError = 0;
    std::vector<std::size_t> offers;
    std::string output;

private:
    asio::io_context& io_;
};

using Result = std::pair<std::error_code, std::size_t>;

Result write(asio::io_context& io, Stream& stream, const std::string& payload)
{
    Result result;
    std::exception_ptr exception;
    unsigned completions = 0;
    const std::array<asio::const_buffer, 2> buffers = {
        asio::buffer(payload.data(), payload.size() / 3),
        asio::buffer(payload.data() + payload.size() / 3, payload.size() - payload.size() / 3)};
    coro_io::async_write(stream, buffers).start([&](async_simple::Try<Result>&& value) {
        ++completions;
        if (value.hasError()) exception = value.getException();
        else result = std::move(value).value();
    });
    require(completions == 0, "completion must remain asynchronous");
    io.run();
    if (exception) std::rethrow_exception(exception);
    require(completions == 1, "write must complete exactly once");
    return result;
}

std::string payload()
{
    std::string bytes(1024 * 1024, '\0');
    for (std::size_t i = 0; i < bytes.size(); ++i) bytes[i] = static_cast<char>(i % 251);
    return bytes;
}

void ideal(std::size_t limit)
{
    asio::io_context io;
    Stream stream(io);
    const auto bytes = payload();
    const auto result = write(io, stream, bytes);
    const auto expected = (bytes.size() + limit - 1) / limit;
    std::cout << "{\"case\":\"ideal\",\"calls\":" << stream.offers.size()
              << ",\"expected_calls\":" << expected << ",\"bytes\":" << result.second << "}" << std::endl;
    require(!result.first && result.second == bytes.size() && stream.output == bytes, "ideal bytes changed");
    require(stream.offers.size() == expected, "ideal write call count differs");
    require(*std::max_element(stream.offers.begin(), stream.offers.end()) <= limit, "write offer exceeds cap");
}

void partial(std::size_t limit)
{
    asio::io_context io;
    Stream stream(io);
    stream.partialLimit = 17003;
    const auto bytes = payload();
    const auto result = write(io, stream, bytes);
    require(!result.first && result.second == bytes.size() && stream.output == bytes, "partial write lost bytes");
    require(stream.offers.size() == (bytes.size() + 17002) / 17003, "partial write call count differs");
    require(*std::max_element(stream.offers.begin(), stream.offers.end()) <= limit, "partial offer exceeds cap");
}

void error(std::size_t limit, bool immediate)
{
    asio::io_context io;
    Stream stream(io);
    stream.partialLimit = 100003;
    stream.errorOnCall = immediate ? 1 : 3;
    stream.bytesOnError = immediate ? 0 : 17;
    const auto bytes = payload();
    const auto result = write(io, stream, bytes);
    const auto expectedBytes = immediate ? 0 : 2 * std::min(limit, stream.partialLimit) + 17;
    require(result.first == asio::error::broken_pipe, "write error changed");
    require(stream.offers.size() == stream.errorOnCall, "write continued after error");
    require(result.second == expectedBytes && stream.output == bytes.substr(0, expectedBytes), "error partial byte count changed");
}

void empty()
{
    asio::io_context io;
    Stream stream(io);
    const auto result = write(io, stream, "");
    require(!result.first && result.second == 0 && stream.output.empty(), "empty completion changed");
    require(stream.offers == std::vector<std::size_t>{0}, "empty write initiation changed");
}

void zeroProgress()
{
    asio::io_context io;
    Stream stream(io);
    stream.partialLimit = 0;
    const auto result = write(io, stream, "nonempty");
    require(!result.first && result.second == 0 && stream.offers.size() == 1, "zero progress must stop");
}
} // namespace

int main(int argc, char** argv)
{
    try {
        const auto limit = argc == 2 ? std::stoull(argv[1]) : 262144;
        require(limit == 65536 || limit == 262144, "unsupported expected cap");
        ideal(limit);
        partial(limit);
        error(limit, false);
        error(limit, true);
        empty();
        zeroProgress();
        std::cout << "{\"verified\":true,\"cases\":6,\"expected_cap\":" << limit << "}" << std::endl;
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << std::endl;
        return 1;
    }
}
