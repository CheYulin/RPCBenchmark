#include "datasystem/kv_client.h"
#include <dlfcn.h>
#include <filesystem>
#include <iostream>
int main(int argc,char **argv) {
 if(argc!=2)return 2;
 Dl_info info{};
 const auto function=&datasystem::KVClient::RegisterCudaFuncs;
 const bool resolved=dladdr(reinterpret_cast<const void*>(function),&info)&&info.dli_fname;
 const bool sdk=resolved&&std::filesystem::canonical(info.dli_fname)==std::filesystem::canonical(argv[1]);
 std::cout << "{\"resolved\":" << (resolved?"true":"false") << ",\"anchor_in_expected_SDK\":" <<(sdk?"true":"false")<<"}"<<std::endl;
 return sdk?0:1;
}
