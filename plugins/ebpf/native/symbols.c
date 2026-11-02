/* Resolve mapped ELF function symbols on the device; never transfer the ELF.
 * Identity is established by /proc maps device+inode and a stable open fd.
 * Build ID is optional: an unstripped file without notes is still usable. */
#define _GNU_SOURCE
#include <elf.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>
#include <signal.h>

static volatile sig_atomic_t scan_expired;
static void scan_timeout(int signal_number){(void)signal_number;scan_expired=1;}

struct mapping { uint64_t start,end,offset,inode; unsigned major,minor; char path[4096]; };
struct frame { uint64_t address,offset; char name[257]; const char *status; };
static int mapping_for(int pid,uint64_t address,struct mapping *m) {
    char filename[64],*line=NULL;size_t cap=0;int found=0;
    snprintf(filename,sizeof(filename),"/proc/%d/maps",pid);
    FILE *f=fopen(filename,"r");if(!f)return 0;
    while(getline(&line,&cap,f)>0) {
        int used=0;char perms[5];struct mapping candidate={0};
        if(sscanf(line,"%"SCNx64"-%"SCNx64" %4s %"SCNx64" %x:%x %"SCNu64" %n",
            &candidate.start,&candidate.end,perms,&candidate.offset,&candidate.major,
            &candidate.minor,&candidate.inode,&used)!=7)continue;
        if(address<candidate.start||address>=candidate.end||!strchr(perms,'x'))continue;
        size_t n=strcspn(line+used,"\n");if(n>=sizeof(candidate.path))break;
        memcpy(candidate.path,line+used,n);candidate.path[n]=0;
        *m=candidate;found=1;break;
    }
    free(line);fclose(f);return found;
}
static int same_map(const struct mapping *a,const struct mapping *b) {
    return a->start==b->start&&a->end==b->end&&a->offset==b->offset&&
        a->inode==b->inode&&a->major==b->major&&a->minor==b->minor&&!strcmp(a->path,b->path);
}
static int matches(const struct stat *s,const struct mapping *m) {
    return S_ISREG(s->st_mode)&&(uint64_t)s->st_ino==m->inode&&
        major(s->st_dev)==m->major&&minor(s->st_dev)==m->minor;
}
static int open_mapping(int pid,const struct mapping *m,struct stat *st) {
    char path[8192];int fd;
    for(int choice=0;choice<3;choice++) {
        if(choice==0)snprintf(path,sizeof(path),"/proc/%d/map_files/%"PRIx64"-%"PRIx64,pid,m->start,m->end);
        else if(choice==1)snprintf(path,sizeof(path),"/proc/%d/exe",pid);
        else {
            if(m->path[0]!='/'||strstr(m->path," (deleted)"))continue;
            snprintf(path,sizeof(path),"/proc/%d/root%s",pid,m->path);
        }
        fd=open(path,O_RDONLY|O_CLOEXEC);if(fd<0)continue;
        if(!fstat(fd,st)&&matches(st,m))return fd;
        close(fd);
    }
    return -1;
}
static int bounded(uint64_t size,uint64_t offset,uint64_t length) {
    return offset<=size&&length<=size-offset;
}
static int stable(const struct stat *a,const struct stat *b) {
    return a->st_dev==b->st_dev&&a->st_ino==b->st_ino&&a->st_size==b->st_size&&
        a->st_mtim.tv_sec==b->st_mtim.tv_sec&&a->st_mtim.tv_nsec==b->st_mtim.tv_nsec&&
        a->st_ctim.tv_sec==b->st_ctim.tv_sec&&a->st_ctim.tv_nsec==b->st_ctim.tv_nsec;
}
static void resolve(int pid,struct frame *frame) {
    struct mapping m,after;struct stat st,final;int fd=-1;void *data=MAP_FAILED;
    frame->status="mapping-unavailable";
    if(!mapping_for(pid,frame->address,&m))return;
    frame->status="file-identity-unavailable";
    fd=open_mapping(pid,&m,&st);if(fd<0)return;
    frame->status="file-too-large";
    if(st.st_size<(off_t)sizeof(Elf64_Ehdr)||st.st_size>128*1024*1024)goto done;
    size_t size=(size_t)st.st_size;data=mmap(NULL,size,PROT_READ,MAP_PRIVATE,fd,0);
    frame->status="file-read-failed";if(data==MAP_FAILED)goto done;
    const unsigned char *bytes=data;Elf64_Ehdr eh;memcpy(&eh,data,sizeof(eh));
    frame->status="unsupported-elf";
    if(memcmp(eh.e_ident,ELFMAG,SELFMAG)||eh.e_ident[EI_CLASS]!=ELFCLASS64||
       eh.e_ident[EI_DATA]!=ELFDATA2LSB||eh.e_machine!=EM_AARCH64||
       (eh.e_type!=ET_EXEC&&eh.e_type!=ET_DYN)||eh.e_phentsize!=sizeof(Elf64_Phdr)||
       eh.e_shentsize!=sizeof(Elf64_Shdr))goto done;
    frame->status="invalid-elf";
    if(!bounded(size,eh.e_phoff,(uint64_t)eh.e_phnum*sizeof(Elf64_Phdr))||
       !bounded(size,eh.e_shoff,(uint64_t)eh.e_shnum*sizeof(Elf64_Shdr)))goto done;
    uint64_t bias=0;int loaded=0;
    for(unsigned i=0;i<eh.e_phnum;i++) {
        Elf64_Phdr ph;memcpy(&ph,bytes+eh.e_phoff+i*sizeof(ph),sizeof(ph));
        if(ph.p_type!=PT_LOAD||ph.p_filesz>UINT64_MAX-ph.p_offset)continue;
        uint64_t file_page=ph.p_offset&~UINT64_C(4095),vpage=ph.p_vaddr&~UINT64_C(4095);
        if(m.offset<file_page||m.offset>=ph.p_offset+ph.p_filesz)continue;
        if(vpage>UINT64_MAX-(m.offset-file_page)||m.start<vpage+m.offset-file_page)continue;
        bias=m.start-(vpage+m.offset-file_page);loaded=1;break;
    }
    frame->status="mapping-offset-mismatch";if(!loaded||frame->address<bias)goto done;
    uint64_t address=frame->address-bias,best=0;int symbol_tables=0,found=0;
    frame->status="no-function-symbol";
    for(unsigned i=0;i<eh.e_shnum;i++) {
        Elf64_Shdr section,strings;memcpy(&section,bytes+eh.e_shoff+i*sizeof(section),sizeof(section));
        if(section.sh_type!=SHT_SYMTAB&&section.sh_type!=SHT_DYNSYM)continue;
        symbol_tables=1;
        if(section.sh_link>=eh.e_shnum||section.sh_entsize!=sizeof(Elf64_Sym)||
           section.sh_size%sizeof(Elf64_Sym)||!bounded(size,section.sh_offset,section.sh_size))continue;
        memcpy(&strings,bytes+eh.e_shoff+section.sh_link*sizeof(strings),sizeof(strings));
        if(!bounded(size,strings.sh_offset,strings.sh_size))continue;
        for(uint64_t j=0;j<section.sh_size/sizeof(Elf64_Sym);j++) {
            Elf64_Sym sym;memcpy(&sym,bytes+section.sh_offset+j*sizeof(sym),sizeof(sym));
            if(ELF64_ST_TYPE(sym.st_info)!=STT_FUNC||!sym.st_shndx||!sym.st_size||
               sym.st_value>address||address-sym.st_value>=sym.st_size||sym.st_name>=strings.sh_size)continue;
            const char *name=(const char*)bytes+strings.sh_offset+sym.st_name;
            const char *end=memchr(name,0,strings.sh_size-sym.st_name);
            if(!end||end==name||end-name>256||(found&&sym.st_value<best))continue;
            memcpy(frame->name,name,(size_t)(end-name));frame->name[end-name]=0;
            frame->offset=address-sym.st_value;best=sym.st_value;found=1;
        }
    }
    frame->status=found?"resolved":symbol_tables?"no-function-symbol":"no-symbol-table";
done:
    if(fstat(fd,&final)||!stable(&st,&final)||!mapping_for(pid,frame->address,&after)||!same_map(&m,&after)) {
        frame->name[0]=0;frame->status="identity-changed";
    }
    if(data!=MAP_FAILED)munmap(data,(size_t)st.st_size);
    close(fd);
}
static void json_string(const char *s) {
    putchar('"');for(const unsigned char *p=(const unsigned char*)s;*p;p++) {
        if(*p=='"'||*p=='\\')printf("\\%c",*p);
        else if(*p<32||*p>=127)printf("\\u%04x",*p);
        else putchar(*p);
    }putchar('"');
}
static void resolve_kernel(struct frame *frames,int count) {
    uint64_t best[127]={0},nearest[127]={0},upper[127];int visible=0;
    for(int i=0;i<count;i++){upper[i]=UINT64_MAX;frames[i].status="kernel-symbol-unavailable";}
    FILE *file=fopen("/proc/kallsyms","r");if(!file)return;
    struct sigaction action={0},previous;action.sa_handler=scan_timeout;sigemptyset(&action.sa_mask);
    sigaction(SIGALRM,&action,&previous);scan_expired=0;alarm(4);
    char line[1024];uint64_t address;
    unsigned lines=0;
    while(!scan_expired&&lines++<300000&&fgets(line,sizeof(line),file)) {
        /* kallsyms is fixed-width here. Avoid sscanf/snprintf per symbol:
         * their repeated format parsing dominates on small ARM devices. */
        size_t length=strlen(line);if(length<20||line[16]!=' '||line[18]!=' ')continue;
        address=0;int valid=1;
        for(int j=0;j<16;j++){
            unsigned char c=(unsigned char)line[j];unsigned value;
            if(c>='0'&&c<='9')value=c-'0';else if(c>='a'&&c<='f')value=c-'a'+10;
            else if(c>='A'&&c<='F')value=c-'A'+10;else{valid=0;break;}
            address=(address<<4)|value;
        }
        if(!valid||!address)continue;
        char type=line[17];size_t name_length=strcspn(line+19," \t\r\n");
        if(!name_length||name_length>256)continue;
        visible=1;
        for(int i=0;i<count;i++) {
            if(address>frames[i].address){if(address<upper[i])upper[i]=address;continue;}
            if(address>nearest[i])nearest[i]=address;
            if(strchr("tTwW",type)&&address>best[i]){
                best[i]=address;memcpy(frames[i].name,line+19,name_length);frames[i].name[name_length]=0;
            }
        }
    }
    int incomplete=scan_expired||ferror(file)||lines>300000;
    alarm(0);sigaction(SIGALRM,&previous,NULL);fclose(file);
    for(int i=0;i<count;i++) {
        if(best[i]&&best[i]==nearest[i]&&upper[i]!=UINT64_MAX&&frames[i].address-best[i]<=1024*1024) {
            frames[i].offset=frames[i].address-best[i];frames[i].status="resolved";
        }else{frames[i].name[0]=0;frames[i].status=incomplete?"kernel-scan-incomplete":visible?"kernel-address-unmapped":"kernel-symbol-unavailable";}
    }
}
int main(int argc,char **argv) {
    if(argc<3||argc>130)return 64;
    char *end;errno=0;long pid=strtol(argv[1],&end,10);
    if(errno||*end||pid<1||pid>4194304)return 64;
    int kernel=!strcmp(argv[2],"--kernel"),first=kernel?3:2,count=argc-first;
    if(count<1||count>127)return 64;
    struct frame frames[127]={0};
    for(int i=first;i<argc;i++) {
        if(strncmp(argv[i],"0x",2)||strlen(argv[i])>18)return 64;
        errno=0;frames[i-first].address=strtoull(argv[i],&end,16);
        if(errno||*end||end==argv[i]+2)return 64;
    }
    if(kernel)resolve_kernel(frames,count);else for(int i=0;i<count;i++)resolve((int)pid,&frames[i]);
    printf("{\"schemaVersion\":1,\"pid\":%ld,\"frames\":[",pid);
    for(int i=0;i<count;i++) {
        struct frame *f=&frames[i];if(i)putchar(',');
        printf("{\"address\":\"0x%"PRIx64"\",\"status\":",f->address);json_string(f->status);
        printf(",\"symbol\":");if(f->name[0])json_string(f->name);else printf("null");
        printf(",\"offset\":%"PRIu64"}",f->offset);
    }
    puts("]}");return 0;
}
