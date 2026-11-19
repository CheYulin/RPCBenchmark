#include "coro_unary.h"
#include <bthread/countdown_event.h>
#include <dlfcn.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <condition_variable>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iomanip>
#include <limits>
#include <memory>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace {
namespace core = datasystem::coro_unary;
using Clock = std::chrono::steady_clock;
constexpr unsigned Owners = 32;
constexpr std::array<std::uint64_t,2> CallsPerOwner{1024,8192};
constexpr auto Purpose = "CLOSED_LOOP32_PARENT_BTHREAD_DISPATCH_COMPONENT_SCREEN_ONLY";
void Require(bool ok, const char *reason) { if (!ok) throw std::runtime_error(reason); }
std::uint64_t NowNs() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now().time_since_epoch()).count();
}
std::string Quote(const std::string &value) {
    constexpr char hex[] = "0123456789abcdef";
    std::string out = "\"";
    for (unsigned char c : value) {
        if (c == '\\' || c == '"') { out += '\\'; out += static_cast<char>(c); }
        else if (c < 32) { out += "\\u00"; out += hex[c >> 4]; out += hex[c & 15]; }
        else out += static_cast<char>(c);
    }
    return out + '"';
}
std::uint64_t Number(const char *text, std::uint64_t low, std::uint64_t high) {
    std::uint64_t value = 0;
    const std::string input(text);
    auto parsed = std::from_chars(input.data(), input.data()+input.size(), value);
    Require(parsed.ec == std::errc{} && parsed.ptr == input.data()+input.size() && value >= low && value <= high,
            "invalid integer argument");
    return value;
}
void WriteNew(const std::filesystem::path &path, const std::string &data) {
    Require(!std::filesystem::exists(path), "evidence file exists");
    std::ofstream out(path); out << data << '\n'; out.close();
    Require(bool(out), "evidence write failed");
}
std::string SdkObject() {
    Dl_info info{};
    Require(dladdr(reinterpret_cast<void *>(&core::NowUs), &info) != 0 && info.dli_fname, "core SDK object unavailable");
    return info.dli_fname;
}
struct CpuSample {
    std::uint64_t read_begin_ns=0,read_end_ns=0,sdk_ticks=0,worker_ticks=0;
};
std::uint64_t CpuTicks(int pid) {
    std::ifstream stream("/proc/"+std::to_string(pid)+"/stat");
    std::string line;
    Require(bool(std::getline(stream,line)),"owned process CPU unavailable");
    const auto close=line.rfind(')');
    Require(close!=std::string::npos && close+2<line.size(),"invalid process stat");
    std::istringstream fields(line.substr(close+2));
    std::string token;std::uint64_t user=0,system=0;
    for(unsigned index=0;index<=12;++index) {
        Require(bool(fields>>token),"short process stat");
        if(index==11)user=std::stoull(token);
        if(index==12)system=std::stoull(token);
    }
    return user+system;
}
CpuSample ReadCpu(int serverPid) {
    CpuSample sample;
    sample.read_begin_ns=NowNs();sample.sdk_ticks=CpuTicks(getpid());
    sample.worker_ticks=CpuTicks(serverPid);sample.read_end_ns=NowNs();
    return sample;
}
std::string CpuJson(const CpuSample &s) {
    std::ostringstream out;
    out<<R"({"read_begin_ns":)"<<s.read_begin_ns<<R"(,"read_end_ns":)"<<s.read_end_ns
       <<R"(,"sdk_ticks":)"<<s.sdk_ticks<<R"(,"worker_ticks":)"<<s.worker_ticks<<"}";
    return out.str();
}
std::string TopologyJson(unsigned prime,unsigned clients) {
    std::vector<std::string> tasks,descriptors;
    {
        for(const auto &entry:std::filesystem::directory_iterator("/proc/self/task"))tasks.push_back(entry.path().filename().string());
        for(const auto &entry:std::filesystem::directory_iterator("/proc/self/fd"))descriptors.push_back(entry.path().filename().string());
    }
    std::sort(tasks.begin(),tasks.end());std::sort(descriptors.begin(),descriptors.end());
    std::ostringstream out;
    out<<R"({"monotonic_ns":)"<<NowNs()<<R"(,"prime_success":)"<<prime
       <<R"(,"client_instances":)"<<clients<<R"(,"expected_cleanup_threads":)"<<clients<<R"(,"self_task_tids":[)";
    for(std::size_t i=0;i<tasks.size();++i)out<<(i?",":"")<<std::stoull(tasks[i]);
    out<<R"(],"self_fds":[)";
    bool first=true;
    for(const auto &name:descriptors) {
        std::error_code error;
        const auto target=std::filesystem::read_symlink("/proc/self/fd/"+name,error);
        if(error==std::errc::no_such_file_or_directory)continue;
        Require(!error,"FD boundary snapshot unavailable");
        out<<(first?"":",")<<R"({"fd":)"<<std::stoull(name)<<R"(,"target":)"<<Quote(target.string())<<"}";first=false;
    }
    out<<R"(],"scope":"boundary snapshot; closed traversal descriptors omitted"})";
    return out.str();
}
struct Prime {
    std::atomic<unsigned> entered{0};
    std::atomic<bool> released{false};
    bthread::CountdownEvent gate{1};
    void Release() { if (!released.exchange(true)) gate.signal(); }
};
int Serve(const std::string &host, const std::filesystem::path &ready) {
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
    Require(server.Start(host, 0).IsOk(), "production core server start failed");
    port.store(server.Port());
    std::ostringstream record;
    record << R"({"purpose":)" << Quote(Purpose) << R"(,"pid":)" << getpid() << R"(,"port":)" << server.Port()
           << R"(,"host":)" << Quote(host) << R"(,"sdk_object":)" << Quote(SdkObject())
           << R"(,"core_io_threads":4,"production_bthread_dispatch":true})";
    WriteNew(ready, record.str());
    std::cout << "READY CORE_OWNER_SERVER " << record.str() << std::endl;
    std::string command;
    const bool quit = !std::getline(std::cin, command) || command == "QUIT";
    prime.Release(); server.Stop();
    std::cout << R"(SERVER_STOPPED {"normal_stop":true,"prime_entered":)" << prime.entered.load() << "}" << std::endl;
    Require(quit, "expected QUIT or EOF");
    return 0;
}
enum class State { Planned, Returned, Cancelled, Exception };
struct Row {
    std::uint64_t id=0, checked=0, begin=0, end=0, observer=0;
    int status=0;
    State state=State::Planned;
    bool attempted=false, bytes=false;
};
struct Owner { long tid=0; bool prime=false; std::array<std::vector<Row>,2> rows; };
struct Group {
    std::mutex mutex; std::condition_variable changed;
    unsigned ready=0, completed=0, generation=0, stage=0;
    bool stop=false;
};
const char *Name(State state) {
    switch (state) {
        case State::Planned: return "unstarted_phase";
        case State::Returned: return "returned";
        case State::Cancelled: return "cancelled";
        case State::Exception: return "exception";
    }
    return "unknown";
}
core::Request MakeRequest(std::uint16_t port, unsigned owner, std::uint64_t id, unsigned phase, bool prime) {
    core::Request request{};
    request.workerPort=port; request.deadlineUs=core::NowUs()+(prime ? 5'000'000 : 1'000'000);
    request.method=prime ? "diag.prime" : "diag.echo";
    request.protobuf=std::to_string(owner)+":"+std::to_string(phase)+":"+std::to_string(id);
    request.attachment.assign(4096, static_cast<char>('a'+owner%26));
    return request;
}
bool Exchange(core::Client &client, core::Request request, Row *row) {
    const auto protobuf=request.protobuf, attachment=request.attachment;
    core::Reply reply{};
    if (row) {
        row->begin=NowNs();
        row->attempted=true;
    }
    const auto status=client.Call(std::move(request),reply);
    if (row) row->end=NowNs();
    const bool ok=status.IsOk() && reply.code==datasystem::K_OK;
    const bool bytes=ok && reply.protobuf==protobuf && reply.attachment==attachment;
    if (row) {
        row->status=status.IsOk() ? static_cast<int>(reply.code) : static_cast<int>(status.GetCode());
        row->bytes=bytes; row->observer=NowNs(); row->state=State::Returned;
    }
    return bytes;
}
void Invoke(unsigned index, Owner &owner, Group &group, core::Client &client,
            std::uint16_t port, std::atomic<bool> &abort) {
    owner.tid=syscall(SYS_gettid);
    std::unique_lock lock(group.mutex);
    ++group.ready; group.changed.notify_all();
    unsigned generation=0;
    for (;;) {
        group.changed.wait(lock,[&]{return group.stop || group.generation!=generation;});
        if (group.stop) return;
        generation=group.generation; const auto stage=group.stage;
        lock.unlock();
        if (stage==0) {
            try { owner.prime=Exchange(client,MakeRequest(port,index,0,0,true),nullptr); }
            catch (...) { owner.prime=false; }
            if (!owner.prime) abort.store(true,std::memory_order_release);
        } else for (auto &row:owner.rows[stage-1]) {
            if (abort.load(std::memory_order_acquire)) {row.state=State::Cancelled;continue;}
            try {
                row.checked=NowNs();
                const bool ok=Exchange(client,MakeRequest(port,index,row.id,stage,false),&row);
                if (!ok) abort.store(true,std::memory_order_release);
            } catch (...) {
                row.state=State::Exception;row.observer=NowNs();abort.store(true,std::memory_order_release);
            }
        }
        lock.lock();++group.completed;group.changed.notify_all();
    }
}
void Stop(Group &group,std::vector<std::thread> &threads) {
    {std::lock_guard lock(group.mutex);group.stop=true;}
    group.changed.notify_all();
    for (auto &thread:threads) if (thread.joinable()) thread.join();
}
std::string PhaseJson(const std::array<Owner,Owners> &owners,unsigned phase,std::uint64_t release,
                      const CpuSample &before,const CpuSample &after) {
    std::uint64_t attempts=0,success=0,cancelled=0,exceptions=0,errors=0,mismatches=0;
    std::uint64_t firstBegin=std::numeric_limits<std::uint64_t>::max(),lastObserver=0;
    std::vector<std::uint64_t> latency;
    for (const auto &owner:owners) for (const auto &row:owner.rows[phase-1]) {
        attempts+=row.attempted;cancelled+=row.state==State::Cancelled || row.state==State::Planned;
        exceptions+=row.state==State::Exception;
        errors+=row.attempted && row.status!=0;mismatches+=row.attempted && row.status==0 && !row.bytes;
        if (row.attempted && row.begin) firstBegin=std::min(firstBegin,row.begin);
        lastObserver=std::max(lastObserver,row.observer);
        if (row.state==State::Returned && row.status==0 && row.bytes) {++success;latency.push_back(row.end-row.begin);}
    }
    const auto planned=CallsPerOwner[phase-1]*Owners;
    const bool complete=release && success==planned && !exceptions;
    const auto elapsed=complete?lastObserver-firstBegin:0;
    std::sort(latency.begin(),latency.end());
    const auto rank=[&](std::uint64_t q){return latency[(latency.size()*q+99)/100-1];};
    std::ostringstream out;
    out << std::setprecision(17) << R"({"phase_id":)"<<phase<<R"(,"epoch_ns":null,"phase_release_ns":)"<<(release?std::to_string(release):"null")
        <<R"(,"planned":)"<<planned<<R"(,"attempts":)"<<attempts<<R"(,"success":)"<<success
        <<R"(,"cancelled":)"<<cancelled<<R"(,"exceptions":)"<<exceptions
        <<R"(,"errors":)"<<errors<<R"(,"mismatches":)"<<mismatches
        <<R"(,"complete":)"<<(complete?"true":"false")
        <<R"(,"core_call_p50_ns":)"<<(complete?std::to_string(rank(50)):"null")
        <<R"(,"core_call_p99_ns":)"<<(complete?std::to_string(rank(99)):"null")
        <<R"(,"first_core_call_begin_ns":)"<<(attempts?std::to_string(firstBegin):"null")
        <<R"(,"last_observer_end_ns":)"<<(lastObserver?std::to_string(lastObserver):"null")
        <<R"(,"actual_cohort_elapsed_ns":)"<<(complete?std::to_string(elapsed):"null")
        <<R"(,"actual_completed_cohort_qps":)";
    if(complete && elapsed)out<<static_cast<double>(success)*1e9/static_cast<double>(elapsed);else out<<"null";
    out<<R"(,"cpu_before":)"<<CpuJson(before)<<R"(,"cpu_after":)"<<CpuJson(after)<<"}";
    return out.str();
}
void SaveRows(const std::filesystem::path &path,const std::array<Owner,Owners> &owners) {
    std::ostringstream out;
    const auto stamp=[](std::uint64_t n){return n?std::to_string(n):"null";};
    for(unsigned phase=1;phase<=2;++phase)for(unsigned owner=0;owner<Owners;++owner)for(const auto &row:owners[owner].rows[phase-1])
        out<<R"({"phase_id":)"<<phase<<R"(,"owner":)"<<owner<<R"(,"tid":)"<<owners[owner].tid
           <<R"(,"arrival_id":)"<<row.id<<R"(,"scheduled_ns":null,"dispatch_checked_ns":null,"unpaced_preparation_begin_ns":)"<<stamp(row.checked)
           <<R"(,"core_call_begin_ns":)"<<stamp(row.begin)
           <<R"(,"core_call_end_ns":)"<<stamp(row.end)<<R"(,"observer_end_ns":)"<<stamp(row.observer)
           <<R"(,"state":)"<<Quote(Name(row.state))<<R"(,"attempted":)"<<(row.attempted?"true":"false")
           <<R"(,"status":)"<<(row.attempted?std::to_string(row.status):"null")
           <<R"(,"bytes_ok":)"<<(row.bytes?"true":"false")<<"}\n";
    WriteNew(path,out.str());
}
int Call(const std::string &host,std::uint16_t port,const std::string &mode,std::uint64_t concurrency,const std::filesystem::path &output) {
    Require(mode=="shared","closed-loop screen requires shared client");
    Require(concurrency==Owners,"closed-loop screen requires concurrency32");
    const char *io=std::getenv("DATASYSTEM_CORO_CALLER_IO");
    Require(io && std::string(io)=="1","DATASYSTEM_CORO_CALLER_IO=1 required");
    const char *serverPidText=std::getenv("DS_CORE_SERVER_PID");
    Require(serverPidText,"DS_CORE_SERVER_PID required");
    const auto serverPid=static_cast<int>(Number(serverPidText,1,std::numeric_limits<int>::max()));
    const auto ticksPerSecond=sysconf(_SC_CLK_TCK);
    Require(ticksPerSecond>0,"clock tick frequency unavailable");
    Require(!std::filesystem::exists(output) && std::filesystem::create_directory(output),"output must be new");
    std::array<Owner,Owners> owners;
    for(unsigned owner=0;owner<Owners;++owner)for(unsigned phase=1;phase<=2;++phase) {
        owners[owner].rows[phase-1].resize(CallsPerOwner[phase-1]);
        for(std::uint64_t round=0;round<CallsPerOwner[phase-1];++round)owners[owner].rows[phase-1][round].id=round*Owners+owner;
    }
    std::vector<std::unique_ptr<core::Client>> clients;
    const unsigned count=1;
    for(unsigned i=0;i<count;++i) {
        clients.emplace_back(std::make_unique<core::Client>(host,port,1000));
        Require(clients.back()->GetInitStatus().IsOk(),"core client configuration failed");
    }
    Group group;std::vector<std::thread> threads;std::atomic<bool> abort{false};
    std::array<std::uint64_t,2> epochs{};std::array<CpuSample,2> before{},after{};std::string failure;
    try {
        for(unsigned i=0;i<Owners;++i)threads.emplace_back(Invoke,i,std::ref(owners[i]),std::ref(group),
            std::ref(*clients[0]),port,std::ref(abort));
        {
            std::unique_lock lock(group.mutex);
            Require(group.changed.wait_for(lock,std::chrono::seconds(10),[&]{return group.ready==Owners;}),"caller startup timeout");
        }
        std::ostringstream ready;
        ready<<R"({"purpose":)"<<Quote(Purpose)<<R"(,"pid":)"<<getpid()<<R"(,"mode":)"<<Quote(mode)
             <<R"(,"host":)"<<Quote(host)<<R"(,"port":)"<<port<<R"(,"sdk_object":)"<<Quote(SdkObject())
             <<R"(,"profile":"closed-loop32-parent-screen","offered_rate_per_second":null,"concurrency":32,"callers":32,"warm_calls_per_caller":1024,"measure_calls_per_caller":8192)"
             <<R"(,"attachment_bytes":4096,"client_instances":)"<<count<<R"(,"expected_cleanup_threads":)"<<count
             <<R"(,"connection_upper_bound":32,"server_pid":)"<<serverPid
             <<R"(,"clock_ticks_per_second":)"<<ticksPerSecond<<R"(,"owner_tids":[)";
        for(unsigned i=0;i<Owners;++i)ready<<(i?",":"")<<owners[i].tid;
        ready<<R"(],"RPC30_accepted":false})";
        WriteNew(output/"ready.json",ready.str());std::cout<<"READY CORE_OWNER_CLIENT "<<ready.str()<<std::endl;
        std::string command;Require(bool(std::getline(std::cin,command)) && command=="GO","expected GO");
        for(unsigned stage=0;stage<=2 && !abort.load();++stage) {
            if(stage) {
                before[stage-1]=ReadCpu(serverPid);
                epochs[stage-1]=NowNs();
            }
            std::unique_lock lock(group.mutex);group.completed=0;group.stage=stage;++group.generation;group.changed.notify_all();
            group.changed.wait(lock,[&]{return group.completed==Owners;});
            if(!stage) {unsigned primed=0;for(const auto &owner:owners)primed+=owner.prime;
                WriteNew(output/"primed.json",TopologyJson(primed,count));
                std::cout<<R"(PRIMED CORE_OWNER_CLIENT {"success":)"<<primed<<"}"<<std::endl;}
            else after[stage-1]=ReadCpu(serverPid);
        }
    }catch(const std::exception &error){failure=error.what();abort.store(true);}
    catch(...){failure="unknown component failure";abort.store(true);}
    Stop(group,threads);clients.clear();SaveRows(output/"all-calls.jsonl",owners);
    WriteNew(output/"after-client-destruction.json",TopologyJson(0,0));
    const bool ok=!abort.load() && failure.empty();
    std::ostringstream result;
    result<<R"({"purpose":)"<<Quote(Purpose)<<R"(,"status":)"<<Quote(ok?"CORE_OWNER_DIAGNOSTIC_COMPLETE":"REJECT_DIAGNOSTIC")
          <<R"(,"mode":)"<<Quote(mode)<<R"(,"quality_valid":)"<<(ok?"true":"false")
          <<R"(,"profile":"closed-loop32-parent-screen","offered_rate_per_second":null,"concurrency":32,"callers":32,"attachment_bytes":4096,"client_instances":)"<<count
          <<R"(,"server_pid":)"<<serverPid<<R"(,"clock_ticks_per_second":)"<<ticksPerSecond
          <<R"(,"expected_cleanup_threads":)"<<count<<R"(,"connection_upper_bound":32,"arrival_schedule_present":false,"per_call_deadline_us":1000000)"
          <<R"(,"normal_join":true,"clients_destroyed":true,"failure":)"<<Quote(failure)
          <<R"(,"phases":[)"<<PhaseJson(owners,1,epochs[0],before[0],after[0])<<","<<PhaseJson(owners,2,epochs[1],before[1],after[1])
          <<R"(],"RPC30_accepted":false,"full_goal_achieved":false})";
    WriteNew(output/"metric.json",result.str());std::cout<<"RESULT "<<result.str()<<std::endl;
    return ok?0:1;
}
}
int main(int argc,char **argv) {
    try {
        if(argc==4 && std::string(argv[1])=="serve")return Serve(argv[2],argv[3]);
        if(argc==7 && std::string(argv[1])=="call-closed")return Call(argv[2],static_cast<std::uint16_t>(Number(argv[3],1,65535)),
            argv[4],Number(argv[5],32,32),argv[6]);
        throw std::invalid_argument("usage: probe serve HOST READY_FILE | probe call-closed HOST PORT shared 32 OUTPUT_DIR");
    }catch(const std::exception &error){std::cerr<<error.what()<<'\n';return 2;}
}
