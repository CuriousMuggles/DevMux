#include <linux/bpf.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <limits.h>
#define I(c,d,s,o,i) { .code=(c), .dst_reg=(d), .src_reg=(s), .off=(o), .imm=(i) }
static int call(int c,union bpf_attr*a){return syscall(__NR_bpf,c,a,sizeof(*a));}
static int map(int type,int k,int v,int n){union bpf_attr a={0};a.map_type=type;a.key_size=k;a.value_size=v;a.max_entries=n;return call(BPF_MAP_CREATE,&a);}
int main(int argc,char **argv){if(argc!=4)return 64;int target=atoi(argv[1]),seconds=atoi(argv[2]);if(target<1||seconds<1||seconds>30)return 64;int stack=map(BPF_MAP_TYPE_STACK_TRACE,4,64*8,4096),result=map(BPF_MAP_TYPE_ARRAY,4,8,1);if(stack<0||result<0){perror("map");return 1;}int pid=target;uint32_t initial_key=0;int64_t initial_value=-99;union bpf_attr init={0};init.map_fd=result;init.key=(uintptr_t)&initial_key;init.value=(uintptr_t)&initial_value;if(call(BPF_MAP_UPDATE_ELEM,&init)<0)return 1;
 struct bpf_insn p[]={I(0xbf,6,1,0,0),I(0x85,0,0,0,BPF_FUNC_get_current_pid_tgid),I(0x77,0,0,0,32),I(0x55,0,0,18,pid),
 I(0xbf,1,6,0,0),I(0x18,2,BPF_PSEUDO_MAP_FD,0,stack),I(0,0,0,0,0),I(0xb7,3,0,0,0),I(0x85,0,0,0,BPF_FUNC_get_stackid),I(0x7b,10,0,-16,0),I(0x62,10,0,-4,0),
 I(0x18,1,BPF_PSEUDO_MAP_FD,0,result),I(0,0,0,0,0),I(0xbf,2,10,0,0),I(0x07,2,0,0,-4),I(0xbf,3,10,0,0),I(0x07,3,0,0,-16),I(0xb7,4,0,0,0),I(0x85,0,0,0,BPF_FUNC_map_update_elem),I(0xb7,0,0,0,0),I(0x95,0,0,0,0)};
 p[3].off=15;p[7].imm=strcmp(argv[3],"user")==0?BPF_F_USER_STACK:0;char log[16384]={0};union bpf_attr a={0};a.prog_type=BPF_PROG_TYPE_RAW_TRACEPOINT;a.insn_cnt=sizeof(p)/sizeof(p[0]);a.insns=(uintptr_t)p;a.license=(uintptr_t)"GPL";a.log_buf=(uintptr_t)log;a.log_size=sizeof(log);a.log_level=1;int prog=call(BPF_PROG_LOAD,&a);if(prog<0){perror("load");puts(log);return 2;}
 memset(&a,0,sizeof(a));a.raw_tracepoint.name=(uintptr_t)"sys_enter";a.raw_tracepoint.prog_fd=prog;int link=call(BPF_RAW_TRACEPOINT_OPEN,&a);if(link<0){perror("attach");return 3;}sleep(seconds);close(link);
 uint32_t key=0;int64_t id=-99;memset(&a,0,sizeof(a));a.map_fd=result;a.key=(uintptr_t)&key;a.value=(uintptr_t)&id;if(call(BPF_MAP_LOOKUP_ELEM,&a)<0){perror("result");return 4;}printf("stack_id=%lld\n",(long long)id);if(id<0)return 5;
 uint64_t frames[64]={0};key=(uint32_t)id;memset(&a,0,sizeof(a));a.map_fd=stack;a.key=(uintptr_t)&key;a.value=(uintptr_t)frames;if(call(BPF_MAP_LOOKUP_ELEM,&a)<0){perror("stack");return 6;}int count=0;for(int i=0;i<64&&frames[i];i++){printf("frame=%016llx\n",(unsigned long long)frames[i]);count++;}printf("frames=%d\n",count);close(prog);close(stack);close(result);return count?0:7;}
