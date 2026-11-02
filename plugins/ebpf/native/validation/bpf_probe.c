#include <linux/bpf.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#define I(c,d,s,o,i) { .code=(c), .dst_reg=(d), .src_reg=(s), .off=(o), .imm=(i) }
static int call(int cmd, union bpf_attr *a) { return syscall(__NR_bpf, cmd, a, sizeof(*a)); }
int main(void) {
 union bpf_attr a; memset(&a,0,sizeof(a)); a.map_type=BPF_MAP_TYPE_ARRAY; a.key_size=4; a.value_size=8; a.max_entries=1;
 int map=call(BPF_MAP_CREATE,&a); if(map<0){perror("map"); return 1;}
 struct bpf_insn p[]={ I(0xbf,2,10,0,0), I(0x07,2,0,0,-4), I(0x62,10,0,-4,0), I(0x18,1,BPF_PSEUDO_MAP_FD,0,map), I(0,0,0,0,0), I(0x85,0,0,0,BPF_FUNC_map_lookup_elem), I(0x15,0,0,2,0), I(0xb7,1,0,0,1), I(0xdb,0,1,0,0), I(0xb7,0,0,0,0), I(0x95,0,0,0,0) };
 char log[16384]={0}; memset(&a,0,sizeof(a)); a.prog_type=BPF_PROG_TYPE_RAW_TRACEPOINT; a.insn_cnt=sizeof(p)/sizeof(p[0]); a.insns=(uintptr_t)p; a.license=(uintptr_t)"GPL"; a.log_buf=(uintptr_t)log; a.log_size=sizeof(log); a.log_level=1;
 int prog=call(BPF_PROG_LOAD,&a); if(prog<0){perror("load"); puts(log);return 2;}
 memset(&a,0,sizeof(a)); a.raw_tracepoint.name=(uintptr_t)"sys_enter"; a.raw_tracepoint.prog_fd=prog; int link=call(BPF_RAW_TRACEPOINT_OPEN,&a); if(link<0){perror("attach");return 3;}
 for(int i=0;i<1000;i++) syscall(__NR_getpid); sleep(1);
 uint32_t key=0; uint64_t value=0; memset(&a,0,sizeof(a)); a.map_fd=map; a.key=(uintptr_t)&key; a.value=(uintptr_t)&value;
 if(call(BPF_MAP_LOOKUP_ELEM,&a)<0){perror("lookup");return 4;} printf("syscall_entries=%llu\n",(unsigned long long)value); close(link);close(prog);close(map);return value>=1000?0:5;
}
