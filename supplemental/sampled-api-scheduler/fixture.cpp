#include "../ds-latency30/capture_helper.h"
#include <cstdio>
#include <cstring>
#include <unistd.h>
extern "C" __attribute__((noinline,noclone,noipa)) void ds_fixture_begin(void* self,unsigned long caller,unsigned long phase,unsigned long arrival,unsigned long scheduled,unsigned long begin,unsigned long methods) {
 __asm__ __volatile__(""::"r"(self),"r"(caller),"r"(phase),"r"(arrival),"r"(scheduled),"r"(begin),"r"(methods):"memory");
}
extern "C" __attribute__((noinline,noclone,noipa)) void ds_fixture_end(void){__asm__ __volatile__("":::"memory");}
int main(int argc,char**argv){
 if(argc==2 && std::strcmp(argv[1],"selector")==0){
  for(unsigned long phase=1;phase<=2;++phase)for(unsigned long id=0;id<(phase==1?50000:150000);++id)
   if(benchmark_capture::Selected(17,phase,id,64))std::printf("%lu %lu\n",phase,id);
  return 0;
 }
 char line[16];std::puts("READY");std::fflush(stdout);if(!std::fgets(line,sizeof(line),stdin))return 2;
 bool first=true;
 for(unsigned long phase=1;phase<=2;++phase)for(unsigned long id=0;id<4096;++id){
  bool selected=benchmark_capture::Selected(17,phase,id,64);
  ds_fixture_begin((void*)0x1234,0,phase,id,0,0,0);
  usleep(selected&&first?50000:selected?100:50);
  if(selected)first=false;
  ds_fixture_end();usleep(100);
 }
 std::puts("DONE");std::fflush(stdout);if(!std::fgets(line,sizeof(line),stdin))return 3;return 0;
}
