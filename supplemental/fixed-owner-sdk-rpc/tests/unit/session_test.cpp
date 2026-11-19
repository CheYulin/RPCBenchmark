#include "capture_helper.h"
#include "capture_observe.h"
#include <array>
#include <barrier>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <sys/syscall.h>
#include <unistd.h>
extern "C" void DsBenchFakeSdkEmitV2(const ds_bench_capture::RpcObservation *) noexcept;
extern "C" const DsBenchApiV2 *DsBenchRpcCaptureV2() noexcept;
namespace {
using benchmark_capture::FixedOwnerConfig;
using benchmark_capture::FixedOwnerPlan;
using benchmark_capture::Session;
std::size_t checks=0;
void Check(bool ok,const char *reason){++checks;if(!ok)throw std::runtime_error(reason);}
std::uint32_t Tid(){return static_cast<std::uint32_t>(syscall(SYS_gettid));}
void Emit(std::uint64_t begin,std::uint32_t count=1){
 for(std::uint32_t i=0;i<count;++i){
  const ds_bench_capture::RpcObservation rpc{begin+i*2,begin+i*2+1,DS_METHOD_GET,false,0,0};DsBenchFakeSdkEmitV2(&rpc);
 }
}
std::filesystem::path Fresh(const std::filesystem::path &root,const std::string &name){auto q=root/name;Check(std::filesystem::create_directory(q),"freshcase");return q;}
void Call(Session &s,std::size_t owner,std::uint64_t phase,std::uint64_t id,std::uint64_t begin,std::uint32_t rpc=1){
 s.Begin(owner,phase,id,begin,begin,benchmark_capture::ReadMethods);if(s.Active())Emit(begin+1,rpc);s.End(owner,begin+100,begin+101,0);
}
bool One(const std::filesystem::path &root,const char *mode,int fault){
 const auto out=Fresh(root,std::string(mode)+"-case-"+std::to_string(fault));
 Check(setenv("DS_BENCH_RPC_CAPTURE_MODE",mode,1)==0,"mode");
 Session s(out,FixedOwnerPlan(FixedOwnerConfig{1,1,1,500,17,1}),true);
 Check(s.Register(0,Tid())&&s.Arm(),"registerarm");
 if(fault==1){s.Begin(0,1,1,10,10,benchmark_capture::ReadMethods);s.End(0,20,20,0);}
 if(fault==2){s.Begin(0,2,0,10,10,benchmark_capture::ReadMethods);s.End(0,20,20,0);}
 if(fault==3){s.Begin(1,1,0,10,10,benchmark_capture::ReadMethods);s.End(1,20,20,0);}
 if(fault==9){s.Begin(0,1,0,20,10,benchmark_capture::ReadMethods);s.End(0,30,30,0);}
 if(fault==4){s.Begin(0,UINT64_C(0x100000001),0,10,10,benchmark_capture::ReadMethods);s.End(0,20,20,0);}
 if(fault==10){s.Begin(0,1,0,100,100,benchmark_capture::ReadMethods);if(s.Active())Emit(101);s.Begin(0,1,0,110,110,benchmark_capture::ReadMethods);s.End(0,200,201,0);}
 else Call(s,0,1,0,100);
 if(fault==5){s.Begin(0,1,0,250,250,benchmark_capture::ReadMethods);s.End(0,260,260,0);}
 s.Drained("warmup",false);
 if(fault==8){s.Begin(0,2,0,300,300,benchmark_capture::ReadMethods);if(s.Active())Emit(301);s.End(0,299,400,0);}
 else if(fault!=6)Call(s,0,2,0,300,fault==7?17:1);
 s.Drained("measure",true);s.BeforeStop(true);s.Unregister(0);const bool good=s.Close();
 Check(s.Good()==good,"closegood");Check(s.Close()==good,"typed close idempotent");
 Check(std::filesystem::exists(out/"fixed-owner-plan.json")&&std::filesystem::exists(out/"fixed-owner-final.json"),"fixed receipts");
 Check(std::filesystem::exists(out/"capture")==bool(std::strcmp(mode,"off")),"off must not create capture");return good;
}
void WrongThread(const std::filesystem::path &root,const char *mode){
 Check(setenv("DS_BENCH_RPC_CAPTURE_MODE",mode,1)==0,"mode");
 Session s(Fresh(root,std::string(mode)+"-wrongthread"),FixedOwnerPlan(FixedOwnerConfig{2,1,1,500,17,1}),true);
 std::barrier phase(3);std::array<bool,2> registered{};std::vector<std::thread> threads;
 for(std::size_t owner=0;owner<2;++owner)threads.emplace_back([&,owner]{
  registered[owner]=s.Register(owner,Tid());phase.arrive_and_wait();phase.arrive_and_wait();
  if(owner==0){s.Begin(1,1,1,10,10,benchmark_capture::ReadMethods);s.End(1,20,20,0);}
  Call(s,owner,1,owner,100+owner*1000);phase.arrive_and_wait();phase.arrive_and_wait();
  Call(s,owner,2,owner,300+owner*1000);phase.arrive_and_wait();phase.arrive_and_wait();s.Unregister(owner);
 });
 phase.arrive_and_wait();const bool armed=s.Arm();phase.arrive_and_wait();phase.arrive_and_wait();s.Drained("warmup",false);phase.arrive_and_wait();phase.arrive_and_wait();s.Drained("measure",true);s.BeforeStop(true);phase.arrive_and_wait();
 for(auto &thread:threads) { thread.join(); }
 Check(registered[0]&&registered[1]&&armed,"thread registration");Check(!s.Close(),"wrong thread must be rejected in off and on");
}
template<class T>T Header(){T t{};t.abi=DS_CAPTURE_ABI;t.bytes=sizeof(T);return t;}
struct CanaryCleanup {
 const DsBenchApiV2 *api;DsCaptureRunV2 *run;void **slot;bool closed=false;
 ~CanaryCleanup(){
  if(closed)return;
  auto snapshot=Header<DsCaptureSnapshotV2>();
  if(*slot && api->snapshot(run,DS_CAPTURE_PARTIAL,&snapshot)==DS_CAPTURE_OK && snapshot.slots[0].active){
   auto end=Header<DsCaptureApiEndV2>();end.api_end_ns=end.validation_end_ns=UINT64_MAX;(void)api->end_api(*slot,&end);
  }
  if(api->snapshot(run,DS_CAPTURE_NORMAL,&snapshot)==DS_CAPTURE_OK){auto seal=Header<DsCaptureSealV2>();(void)api->disarm(run,&snapshot,&seal);}
  if(*slot)(void)api->unregister_caller(*slot);
  if(api->close(run)!=DS_CAPTURE_OK){std::fputs("CANARY_FIXTURE_UNRESOLVED\n",stderr);std::_Exit(4);}
 }
};
void Canary17(){
 const auto *api=DsBenchRpcCaptureV2();Check(api&&api->abi==DS_CAPTURE_ABI,"provider ABI");
 auto config=Header<DsCaptureRunConfigV2>();config.callers=1;config.mode=DS_CAPTURE_ENABLED;config.normal_records=16;config.error_records=1;config.normal_ends=1;config.error_ends=1;config.max_rpc_per_api=16;config.sample_modulus=1;config.sample_seed=17;config.memory_budget_bytes=128*1024*1024;
 struct Records{std::uint64_t before=UINT64_C(0x123456789abcdef0);std::array<DsCaptureSampleV2,17> data{};std::uint64_t after=UINT64_C(0xfedcba9876543210);}records;
 struct Ends{std::uint64_t before=UINT64_C(0x1020304050607080);std::array<DsCaptureApiSummaryV2,2> data{};std::uint64_t after=UINT64_C(0x8070605040302010);}ends;
 auto run=Header<DsCaptureRunV2>();Check(api->open(&config,&run)==DS_CAPTURE_OK,"canary open");void *slot=nullptr;CanaryCleanup cleanup{api,&run,&slot};
 auto buffers=Header<DsCaptureBuffersV2>();buffers.records=records.data.data();buffers.record_capacity=17;buffers.ends=ends.data.data();buffers.end_capacity=2;
 Check(api->register_caller(&run,0,Tid(),&buffers,&slot)==DS_CAPTURE_OK,"canary register");auto arm=Header<DsCaptureArmV2>();arm.expected_callers=1;arm.generation=run.generation;Check(api->arm(&run,&arm)==DS_CAPTURE_OK,"canary arm");
 auto begin=Header<DsCaptureApiContextV2>();begin.generation=run.generation;begin.phase_id=1;begin.arrival_id=0;begin.scheduled_ns=100;begin.api_begin_ns=100;begin.required_methods=benchmark_capture::ReadMethods;Check(api->begin_api(slot,&begin)==DS_CAPTURE_OK,"canary begin");Emit(101,17);
 auto end=Header<DsCaptureApiEndV2>();end.api_end_ns=200;end.validation_end_ns=201;Check(api->end_api(slot,&end)==DS_CAPTURE_OK,"canary end");
 auto snapshot=Header<DsCaptureSnapshotV2>();Check(api->snapshot(&run,DS_CAPTURE_NORMAL,&snapshot)==DS_CAPTURE_OK,"canary snapshot");Check(snapshot.slots[0].rpc_count==17&&(snapshot.slots[0].errors&DS_ERROR_ORDINAL),"17th RPC invalidates without silence");
 Check(records.before==UINT64_C(0x123456789abcdef0)&&records.after==UINT64_C(0xfedcba9876543210)&&ends.before==UINT64_C(0x1020304050607080)&&ends.after==UINT64_C(0x8070605040302010),"capture canary overwritten");
 auto seal=Header<DsCaptureSealV2>();Check(api->disarm(&run,&snapshot,&seal)==DS_CAPTURE_OK&&!seal.valid_capture,"invalid capture seal");Check(api->unregister_caller(slot)==DS_CAPTURE_OK,"canary unregister");Check(api->close(&run)==DS_CAPTURE_OK,"canary close");cleanup.closed=true;
}
}
int main(int argc,char **argv){
 try{
  Check(argc==3,"output andfakeSDK arguments");const std::filesystem::path out(argv[1]);Check(std::filesystem::create_directory(out),"outputfresh");
  Check(setenv("DS_BENCH_RPC_CAPTURE_SDK",argv[2],1)==0&&setenv("DS_BENCH_RPC_CAPTURE_SEED","17",1)==0&&setenv("DS_BENCH_RPC_CAPTURE_MODULUS","1",1)==0,"env");
  static_assert(sizeof(Session)==sizeof(void*));
  for(const char *mode:{"off","enabled"}){
   for(int fault=0;fault<=10;++fault) { if(fault==7)continue;Check(One(out,mode,fault)==(fault==0),"fixed arrival contract"); }
   WrongThread(out,mode);
  }
  Check(!One(out,"enabled",7),"17 RPC invalid capture");Canary17();
  Check(setenv("DS_BENCH_RPC_CAPTURE_MODE","off",1)==0,"mode");
  {Session legacy(Fresh(out,"legacy-default-off"),1,1,1,true);Check(legacy.Register(0,Tid())&&legacy.Arm(),"legacy default");legacy.Begin(0,1,999,10,10,benchmark_capture::ReadMethods);legacy.End(0,20,20,0);legacy.Unregister(0);Check(legacy.Close(),"legacy behavior changed");}
  {Check(setenv("DS_BENCH_RPC_CAPTURE_MODE","enabled",1)==0,"mode");Session budget(Fresh(out,"default-full-capacity"),FixedOwnerPlan(FixedOwnerConfig{}),true);Check(budget.Active(),"default full capture capacity must fit");}
  std::cout<<"FIXED_OWNER_SESSION_TESTS_PASS checks="<<checks<<" fake_provider_only=1 realSDK_network=0\n";return 0;
 }catch(const std::exception&e){std::cerr<<"SESSION_TEST_FAIL "<<e.what()<<'\n';return 1;}
}
