#include <linux/bpf.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include <stdlib.h>
#define I(c,d,s,o,i) { .code=(c), .dst_reg=(d), .src_reg=(s), .off=(o), .imm=(i) }
static int call(int c,union bpf_attr *a){return syscall(__NR_bpf,c,a,sizeof(*a));}
static int mkmap(int type,int k,int v,int n){union bpf_attr a={0};a.map_type=type;a.key_size=k;a.value_size=v;a.max_entries=n;return call(BPF_MAP_CREATE,&a);}
static int load(struct bpf_insn*p,int n){char log[16384]={0};union bpf_attr a={0};a.prog_type=BPF_PROG_TYPE_RAW_TRACEPOINT;a.insn_cnt=n;a.insns=(uintptr_t)p;a.license=(uintptr_t)"GPL";a.log_buf=(uintptr_t)log;a.log_size=sizeof(log);a.log_level=1;int fd=call(BPF_PROG_LOAD,&a);if(fd<0){perror("load");puts(log);}return fd;}
static int attach(int fd,const char*n){union bpf_attr a={0};a.raw_tracepoint.name=(uintptr_t)n;a.raw_tracepoint.prog_fd=fd;int r=call(BPF_RAW_TRACEPOINT_OPEN,&a);if(r<0)perror(n);return r;}
int main(int argc,char **argv){if(argc!=3)return 64;int target=atoi(argv[1]),seconds=atoi(argv[2]);if(target<1||seconds<1||seconds>30)return 64;int start=mkmap(BPF_MAP_TYPE_HASH,4,8,64), hist=mkmap(BPF_MAP_TYPE_ARRAY,4,8,32);if(start<0||hist<0){perror("map");return 1;} int pid=target;
 struct bpf_insn en[]={
 I(0x85,0,0,0,BPF_FUNC_get_current_pid_tgid),I(0x63,10,0,-4,0),I(0x77,0,0,0,32),I(0x55,0,0,10,pid),
 I(0x85,0,0,0,BPF_FUNC_ktime_get_ns),I(0x7b,10,0,-16,0),I(0x18,1,BPF_PSEUDO_MAP_FD,0,start),I(0,0,0,0,0),I(0xbf,2,10,0,0),I(0x07,2,0,0,-4),I(0xbf,3,10,0,0),I(0x07,3,0,0,-16),I(0xb7,4,0,0,0),I(0x85,0,0,0,BPF_FUNC_map_update_elem),I(0xb7,0,0,0,0),I(0x95,0,0,0,0)};
 struct bpf_insn ex[]={
 I(0x85,0,0,0,BPF_FUNC_get_current_pid_tgid),I(0x63,10,0,-4,0),I(0x77,0,0,0,32),I(0x55,0,0,22,pid),
 I(0x18,1,BPF_PSEUDO_MAP_FD,0,start),I(0,0,0,0,0),I(0xbf,2,10,0,0),I(0x07,2,0,0,-4),I(0x85,0,0,0,BPF_FUNC_map_lookup_elem),I(0x15,0,0,16,0),I(0x79,6,0,0,0),I(0x85,0,0,0,BPF_FUNC_ktime_get_ns),I(0x1f,0,6,0,0),I(0x77,0,0,0,16),
 I(0x35,0,0,1,31),I(0x05,0,0,1,0),I(0xb7,0,0,0,31),I(0x63,10,0,-8,0),I(0x18,1,BPF_PSEUDO_MAP_FD,0,hist),I(0,0,0,0,0),I(0xbf,2,10,0,0),I(0x07,2,0,0,-8),I(0x85,0,0,0,BPF_FUNC_map_lookup_elem),I(0x15,0,0,2,0),I(0xb7,1,0,0,1),I(0xdb,0,1,0,0),I(0xb7,0,0,0,0),I(0x95,0,0,0,0)};
 int e=load(en,sizeof(en)/sizeof(en[0])),x=load(ex,sizeof(ex)/sizeof(ex[0]));if(e<0||x<0)return 2;int l1=attach(e,"sys_enter"),l2=attach(x,"sys_exit");if(l1<0||l2<0)return 3;
 sleep(seconds);close(l1);close(l2);uint64_t total=0, overflow=0;for(uint32_t k=0;k<32;k++){uint64_t v=0;union bpf_attr a={0};a.map_fd=hist;a.key=(uintptr_t)&k;a.value=(uintptr_t)&v;if(call(BPF_MAP_LOOKUP_ELEM,&a)<0){perror("lookup");return 4;}if(v){printf("bucket=%u (width 65536 ns, 31=overflow): %llu\n",k,(unsigned long long)v);total+=v;if(k==31)overflow=v;}}printf("samples=%llu overflow=%llu\n",(unsigned long long)total,(unsigned long long)overflow);close(e);close(x);close(start);close(hist);return 0;}
