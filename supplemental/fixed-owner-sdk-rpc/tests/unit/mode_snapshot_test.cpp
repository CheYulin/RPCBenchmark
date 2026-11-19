#include "capture_helper.h"
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <stdexcept>
int main(int argc,char **argv) {
 try {
  if(argc!=3)throw std::runtime_error("output and fake SDK arguments");
  if(!std::filesystem::create_directory(argv[1]))throw std::runtime_error("outputfresh");
  static char setting[]="DS_BENCH_RPC_CAPTURE_MODE=enabled";
  if(putenv(setting)||setenv("DS_BENCH_RPC_CAPTURE_SDK",argv[2],1)||setenv("DS_BENCH_RPC_CAPTURE_SEED","17",1)||setenv("DS_BENCH_RPC_CAPTURE_MODULUS","1",1))throw std::runtime_error("env");
  bool captured=false;
  {
   benchmark_capture::Session s(argv[1],benchmark_capture::FixedOwnerPlan(benchmark_capture::FixedOwnerConfig{1,1,1,500,17,1}),true);
   std::strcpy(std::strchr(setting,'=')+1,"off");
   captured=s.Active()&&std::strcmp(s.Mode(),"enabled")==0;
  }
  if(!captured)throw std::runtime_error("Session must own immutable mode after constructor");
  std::cout<<"FIXED_OWNER_MODE_SNAPSHOT_PASS normal_unarmed_destructor=1\n";return 0;
 }catch(const std::exception&e){std::cerr<<"MODE_SNAPSHOT_FAIL "<<e.what()<<'\n';return 1;}
}
