/* Bounded correlated raw-syscall collector. No BTF or runtime compiler needed. */
#include <linux/bpf.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
struct asmcode {struct bpf_insn p[512];int n;};
static void emit(struct asmcode*a,int c,int d,int s,int o,int i){a->p[a->n++]=(struct bpf_insn){.code=c,.dst_reg=d,.src_reg=s,.off=o,.imm=i};}
static int jump(struct asmcode*a,int c,int d,int i){int n=a->n;emit(a,c,d,0,0,i);return n;}
static void patch(struct asmcode*a,int n){a->p[n].off=a->n-n-1;}
static void mapfd(struct asmcode*a,int r,int fd){emit(a,0x18,r,BPF_PSEUDO_MAP_FD,0,fd);emit(a,0,0,0,0,0);}
static void ptr(struct asmcode*a,int r,int off){emit(a,0xbf,r,10,0,0);emit(a,0x07,r,0,0,off);}
static void lookup(struct asmcode*a,int fd,int key){mapfd(a,1,fd);ptr(a,2,key);emit(a,0x85,0,0,0,BPF_FUNC_map_lookup_elem);}
static void update(struct asmcode*a,int fd,int key,int value){mapfd(a,1,fd);ptr(a,2,key);ptr(a,3,value);emit(a,0xb7,4,0,0,0);emit(a,0x85,0,0,0,BPF_FUNC_map_update_elem);}
static void count(struct asmcode*a,int fd,int key){emit(a,0x62,10,0,-8,key);lookup(a,fd,-8);int skip=jump(a,0x15,0,0);emit(a,0xb7,1,0,0,1);emit(a,0xdb,0,1,0,0);patch(a,skip);}
static int bpf(int cmd,union bpf_attr*a){return syscall(__NR_bpf,cmd,a,sizeof(*a));}
static int map(int type,int key,int size,int entries){union bpf_attr a={0};a.map_type=type;a.key_size=key;a.value_size=size;a.max_entries=entries;return bpf(BPF_MAP_CREATE,&a);}
static int load(struct asmcode*p){char log[32768]={0};union bpf_attr a={0};a.prog_type=BPF_PROG_TYPE_RAW_TRACEPOINT;a.insns=(uintptr_t)p->p;a.insn_cnt=p->n;a.license=(uintptr_t)"GPL";a.log_buf=(uintptr_t)log;a.log_size=sizeof(log);a.log_level=1;int fd=bpf(BPF_PROG_LOAD,&a);if(fd<0){perror("load");fputs(log,stderr);}return fd;}
static int attach(int fd,char*name){union bpf_attr a={0};a.raw_tracepoint.prog_fd=fd;a.raw_tracepoint.name=(uintptr_t)name;return bpf(BPF_RAW_TRACEPOINT_OPEN,&a);}
static int get(int fd,uint32_t key,void*value){union bpf_attr a={0};a.map_fd=fd;a.key=(uintptr_t)&key;a.value=(uintptr_t)value;return bpf(BPF_MAP_LOOKUP_ELEM,&a);}
static uint64_t now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return (uint64_t)t.tv_sec*1000000000+t.tv_nsec;}
static void frames(int fd,int64_t id){uint64_t values[64]={0};if(id<0||get(fd,(uint32_t)id,values)<0){printf("null");return;}printf("[");int n=0;for(int i=0;i<64&&values[i];i++){printf("%s\"0x%llx\"",n++?",":"",(unsigned long long)values[i]);}printf("]");}
static int probe_range(int fd,const char *name,int comma){
 uint64_t address=0;uint32_t length=0;struct bpf_prog_info info={0};union bpf_attr attr={0};
 info.nr_jited_ksyms=1;info.jited_ksyms=(uintptr_t)&address;
 info.nr_jited_func_lens=1;info.jited_func_lens=(uintptr_t)&length;
 attr.info.bpf_fd=fd;attr.info.info_len=sizeof(info);attr.info.info=(uintptr_t)&info;
 if(bpf(BPF_OBJ_GET_INFO_BY_FD,&attr)<0||!address||!length||length>1048576)return 0;
 printf("%s{\"name\":\"%s\",\"start\":\"0x%llx\",\"size\":%u}",comma?",":"",name,(unsigned long long)address,length);return 1;
}
int main(int argc,char**argv){
 if(argc!=5)return 64;char*tail;long pid=strtol(argv[1],&tail,10);if(*tail||pid<1||pid>4194304)return 64;long seconds=strtol(argv[2],&tail,10);if(*tail||seconds<1||seconds>30)return 64;long threshold=strtol(argv[3],&tail,10);if(*tail||threshold<1||threshold>1000000000)return 64;if(strlen(argv[4])!=32||strspn(argv[4],"0123456789abcdef")!=32)return 64;
 int starts=map(BPF_MAP_TYPE_HASH,4,32,4096),stacks=map(BPF_MAP_TYPE_STACK_TRACE,4,512,4096),stats=map(BPF_MAP_TYPE_ARRAY,4,8,5),last=map(BPF_MAP_TYPE_HASH,4,48,1),hist=map(BPF_MAP_TYPE_ARRAY,4,8,32);
 if(starts<0||stacks<0||stats<0||last<0||hist<0){perror("map");return 1;}
 struct asmcode en={0},ex={0};
 emit(&en,0xbf,6,1,0,0);emit(&en,0x85,0,0,0,BPF_FUNC_get_current_pid_tgid);emit(&en,0x63,10,0,-4,0);emit(&en,0x77,0,0,0,32);int ef=jump(&en,0x55,0,pid);
 emit(&en,0x85,0,0,0,BPF_FUNC_ktime_get_ns);emit(&en,0x7b,10,0,-64,0);emit(&en,0x79,0,6,8,0);emit(&en,0x7b,10,0,-56,0);
 for(int user=0;user<2;user++){emit(&en,0xbf,1,6,0,0);mapfd(&en,2,stacks);emit(&en,0xb7,3,0,0,user?BPF_F_USER_STACK:0);emit(&en,0x85,0,0,0,BPF_FUNC_get_stackid);emit(&en,0x7b,10,0,user?-40:-48,0);int ok=jump(&en,0x75,0,0);count(&en,stats,3);patch(&en,ok);}
 update(&en,starts,-4,-64);int saved=jump(&en,0x15,0,0);count(&en,stats,2);patch(&en,saved);patch(&en,ef);emit(&en,0xb7,0,0,0,0);emit(&en,0x95,0,0,0,0);
 emit(&ex,0x85,0,0,0,BPF_FUNC_get_current_pid_tgid);emit(&ex,0x63,10,0,-4,0);emit(&ex,0x77,0,0,0,32);int xf=jump(&ex,0x55,0,pid);lookup(&ex,starts,-4);int missing=jump(&ex,0x15,0,0);emit(&ex,0xbf,6,0,0,0);
 for(int i=0;i<4;i++){emit(&ex,0x79,0,6,i*8,0);emit(&ex,0x7b,10,0,-64+i*8,0);}
 emit(&ex,0x85,0,0,0,BPF_FUNC_ktime_get_ns);emit(&ex,0x7b,10,0,-32,0);emit(&ex,0xbf,8,0,0,0);emit(&ex,0x79,7,10,-64,0);emit(&ex,0x1f,8,7,0,0);emit(&ex,0x61,0,10,-4,0);emit(&ex,0x7b,10,0,-24,0);count(&ex,stats,0);
 emit(&ex,0xbf,0,8,0,0);emit(&ex,0x77,0,0,0,16);int below=jump(&ex,0xa5,0,31);emit(&ex,0xb7,0,0,0,31);patch(&ex,below);emit(&ex,0x63,10,0,-8,0);lookup(&ex,hist,-8);int hskip=jump(&ex,0x15,0,0);emit(&ex,0xb7,1,0,0,1);emit(&ex,0xdb,0,1,0,0);patch(&ex,hskip);
 int fast=jump(&ex,0xa5,8,threshold);count(&ex,stats,4);emit(&ex,0x62,10,0,-8,0);update(&ex,last,-8,-64);int stored=jump(&ex,0x15,0,0);count(&ex,stats,2);patch(&ex,stored);patch(&ex,fast);
 mapfd(&ex,1,starts);ptr(&ex,2,-4);emit(&ex,0x85,0,0,0,BPF_FUNC_map_delete_elem);int done=jump(&ex,0x05,0,0);patch(&ex,missing);count(&ex,stats,1);patch(&ex,done);patch(&ex,xf);emit(&ex,0xb7,0,0,0,0);emit(&ex,0x95,0,0,0,0);
 int ep=load(&en),xp=load(&ex);if(ep<0||xp<0)return 2;uint64_t start=now();int xl=attach(xp,"sys_exit"),el=attach(ep,"sys_enter");if(xl<0||el<0){perror("attach");return 3;}sleep(seconds);close(el);close(xl);uint64_t end=now(),q[5]={0},event[6]={0};for(int i=0;i<5;i++)if(get(stats,i,&q[i])<0)return 4;int present=get(last,0,event)==0;
 printf("{\"schemaVersion\":1,\"sessionId\":\"%s\",\"pid\":%ld,\"startNs\":%llu,\"endNs\":%llu,\"slowThresholdNs\":%ld,\"quality\":{\"completedCalls\":%llu,\"unmatchedExits\":%llu,\"mapUpdateFailures\":%llu,\"stackFailures\":%llu,\"droppedSlowCalls\":%llu},\"latencyBuckets\":[",argv[4],pid,(unsigned long long)start,(unsigned long long)end,threshold,(unsigned long long)q[0],(unsigned long long)q[1],(unsigned long long)q[2],(unsigned long long)q[3],(unsigned long long)(q[4]-(present?1:0)));
 for(int i=0;i<32;i++){uint64_t n=0;if(get(hist,i,&n)<0)return 4;printf("%s%llu",i?",":"",(unsigned long long)n);}printf("],\"bucketWidthNs\":65536,\"slowCalls\":[");
 if(present){printf("{\"sessionId\":\"%s\",\"pid\":%ld,\"tid\":%u,\"syscallId\":%llu,\"enterNs\":%llu,\"exitNs\":%llu,\"kernelStack\":",argv[4],pid,(unsigned)event[5],(unsigned long long)event[1],(unsigned long long)event[0],(unsigned long long)event[4]);frames(stacks,(int64_t)event[2]);printf(",\"userStack\":");frames(stacks,(int64_t)event[3]);printf("}");}
 printf("],\"kernelProbeRanges\":[");int probes=probe_range(ep,"devmux_sys_enter",0);probe_range(xp,"devmux_sys_exit",probes);puts("]}");return 0;
}
