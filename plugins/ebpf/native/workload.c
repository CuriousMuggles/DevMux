/* ARM64 integration workload: known call chain, short calls and 5 ms waits. */
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>
static volatile sig_atomic_t stopped;
static void stop(int signal_number) { (void)signal_number; stopped = 1; }
__attribute__((noinline)) static long short_call(void) {
    volatile long result = syscall(__NR_getpid);
    return result;
}
__attribute__((noinline)) static void delayed_call(void) {
    struct timespec duration = {0, 5000000};
    syscall(__NR_nanosleep, &duration, NULL);
}
__attribute__((noinline)) static void test_cycle(void) {
    for (int i = 0; i < 100; i++) short_call();
    delayed_call();
}
int main(int argc, char **argv) {
    int seconds = argc > 1 ? atoi(argv[1]) : 180;
    if (seconds < 1 || seconds > 3600) return 64;
    signal(SIGINT, stop); signal(SIGTERM, stop);
    printf("workload_pid=%ld duration_seconds=%d\n", (long)getpid(), seconds);
    fflush(stdout);
    struct timespec start, now;
    clock_gettime(CLOCK_MONOTONIC, &start);
    do {
        test_cycle();
        clock_gettime(CLOCK_MONOTONIC, &now);
    } while (!stopped && now.tv_sec - start.tv_sec < seconds);
    puts("workload_finished");
    return 0;
}
