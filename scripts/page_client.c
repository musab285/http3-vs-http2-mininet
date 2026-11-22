#define _POSIX_C_SOURCE 200809L
#include <curl/curl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/stat.h>

#define COUNT 100
struct Request { CURL *easy; int id; char url[2048]; size_t bytes; double sent; double finished; };
static FILE *events;
static long long origin_ns, epoch_ns;
static long long nanoseconds(clockid_t clock) { struct timespec t; clock_gettime(clock,&t); return (long long)t.tv_sec*1000000000LL+t.tv_nsec; }
static double now(void) { return (nanoseconds(CLOCK_MONOTONIC)-origin_ns)/1e9; }
static void event(struct Request *r,const char *name,size_t bytes) {
    long long delta=nanoseconds(CLOCK_MONOTONIC)-origin_ns;
    fprintf(events,"%lld,%.9f,%s,%d,%zu,%zu\n",epoch_ns+delta,delta/1e9,name,r->id,bytes,r->bytes);
}
static size_t receive(char *data,size_t size,size_t n,void *user) {
    (void)data; struct Request *r=user; size_t bytes=size*n;
    r->bytes+=bytes; event(r,"data",bytes); return bytes;
}
static int debug(CURL *easy,curl_infotype type,char *data,size_t size,void *user) {
    (void)easy; (void)data; (void)size;
    struct Request *r=user;
    if(type==CURLINFO_HEADER_OUT && r->sent<0) { r->sent=now(); event(r,"request_sent",0); }
    return 0;
}
static void check(CURLcode code) { if(code!=CURLE_OK) { fprintf(stderr,"libcurl: %s\n",curl_easy_strerror(code)); exit(2); } }
static void multi_check(CURLMcode code) { if(code!=CURLM_OK) { fprintf(stderr,"multi: %s\n",curl_multi_strerror(code)); exit(2); } }
static int compare(const void *a,const void *b) { double x=*(const double*)a,y=*(const double*)b; return (x>y)-(x<y); }
int main(int argc,char **argv) {
    if(argc!=7) { fprintf(stderr,"Usage: page_client h2|h3 CA URLS OUTPUT CONTENT TIMEOUT\n"); return 2; }
    int h3=strcmp(argv[1],"h3")==0;
    if(!h3 && strcmp(argv[1],"h2")) return 2;
    check(curl_global_init(CURL_GLOBAL_DEFAULT));
    if(!(curl_version_info(CURLVERSION_NOW)->features & CURL_VERSION_HTTP3)) { fprintf(stderr,"HTTP3 unavailable\n"); return 2; }
    char path[4096]; snprintf(path,sizeof(path),"%s/app-events.csv",argv[4]); events=fopen(path,"w");
    if(!events) { perror(path);return 2; }
    setvbuf(events,NULL,_IOFBF,1024*1024);
    fprintf(events,"epoch_ns,time_s,event,request_id,chunk_bytes,total_bytes\n");
    FILE *urls=fopen(argv[3],"r"); if(!urls) return 2;
    origin_ns=nanoseconds(CLOCK_MONOTONIC); epoch_ns=nanoseconds(CLOCK_REALTIME);
    CURLM *multi=curl_multi_init(); if(!multi) return 2;
    multi_check(curl_multi_setopt(multi,CURLMOPT_MAX_HOST_CONNECTIONS,1L));
    multi_check(curl_multi_setopt(multi,CURLMOPT_PIPELINING,(long)CURLPIPE_MULTIPLEX));
    struct Request requests[COUNT]={0}; int count=0;
    while(count<COUNT && fgets(requests[count].url,sizeof(requests[count].url),urls)) {
        struct Request *r=&requests[count]; r->url[strcspn(r->url,"\r\n")]=0;
        if(!r->url[0]) continue;
        r->id=count; r->sent=-1; r->easy=curl_easy_init(); if(!r->easy) return 2;
        check(curl_easy_setopt(r->easy,CURLOPT_URL,r->url));
        check(curl_easy_setopt(r->easy,CURLOPT_CAINFO,argv[2]));
        check(curl_easy_setopt(r->easy,CURLOPT_NOPROXY,"*"));
        check(curl_easy_setopt(r->easy,CURLOPT_HTTP_VERSION,(long)(h3?CURL_HTTP_VERSION_3ONLY:CURL_HTTP_VERSION_2TLS)));
        check(curl_easy_setopt(r->easy,CURLOPT_PIPEWAIT,1L));
        check(curl_easy_setopt(r->easy,CURLOPT_TIMEOUT,strtol(argv[6],NULL,10)));
        check(curl_easy_setopt(r->easy,CURLOPT_WRITEFUNCTION,receive));
        check(curl_easy_setopt(r->easy,CURLOPT_WRITEDATA,r));
        check(curl_easy_setopt(r->easy,CURLOPT_PRIVATE,r));
        check(curl_easy_setopt(r->easy,CURLOPT_VERBOSE,1L));
        check(curl_easy_setopt(r->easy,CURLOPT_DEBUGFUNCTION,debug));
        check(curl_easy_setopt(r->easy,CURLOPT_DEBUGDATA,r));
        event(r,"queued",0); multi_check(curl_multi_add_handle(multi,r->easy)); count++;
    }
    fclose(urls); if(count!=COUNT) { fprintf(stderr,"Expected 100 URLs\n"); return 2; }
    int running=0,done=0,failed=0; long connects=0; curl_off_t connection=-1;
    double durations[COUNT],first=1e30,last=0;
    snprintf(path,sizeof(path),"%s/requests.tsv",argv[4]);FILE *timings=fopen(path,"w");if(!timings) return 2;
    snprintf(path,sizeof(path),"%s/request-map.csv",argv[4]);FILE *mapping=fopen(path,"w");if(!mapping)return 2;
    fprintf(mapping,"request_id,url,connection_id,bytes,status\n");
    do {
        multi_check(curl_multi_perform(multi,&running));
        int remaining; CURLMsg *message;
        while((message=curl_multi_info_read(multi,&remaining))) {
            if(message->msg!=CURLMSG_DONE)continue;
            struct Request *r=NULL;check(curl_easy_getinfo(message->easy_handle,CURLINFO_PRIVATE,&r));
            r->finished=now();event(r,"complete",0);
            long status,version,new_connections;curl_off_t cid;
            check(curl_easy_getinfo(r->easy,CURLINFO_RESPONSE_CODE,&status));
            check(curl_easy_getinfo(r->easy,CURLINFO_HTTP_VERSION,&version));
            check(curl_easy_getinfo(r->easy,CURLINFO_NUM_CONNECTS,&new_connections));
            check(curl_easy_getinfo(r->easy,CURLINFO_CONN_ID,&cid));connects+=new_connections;
            if(connection<0)connection=cid;
            const char *object=strstr(r->url,"/objects/"); struct stat info;
            int valid=object!=NULL;
            if(valid) { snprintf(path,sizeof(path),"%s%s",argv[5],object);valid=stat(path,&info)==0 && (size_t)info.st_size==r->bytes; }
            if(message->data.result!=CURLE_OK || status!=200 || version!=(h3?CURL_HTTP_VERSION_3:CURL_HTTP_VERSION_2_0) || cid!=connection || r->sent<0 || !valid) {
                failed++;fprintf(stderr,"Request %d failed: %s status=%ld version=%ld bytes=%zu\n",r->id,curl_easy_strerror(message->data.result),status,version,r->bytes);
            }
            double start=r->sent<0?0:r->sent;
            durations[done++]=r->finished-start;
            if(start<first) first=start;
            if(r->finished>last) last=r->finished;
            fprintf(timings,"%.0f\t%ld\t%.0f\n",epoch_ns/1000.0+start*1e6,status,(r->finished-start)*1e6);
            fprintf(mapping,"%d,%s,%lld,%zu,%ld\n",r->id,r->url,(long long)cid,r->bytes,status);
            multi_check(curl_multi_remove_handle(multi,r->easy));curl_easy_cleanup(r->easy);
        }
        if(running)multi_check(curl_multi_poll(multi,NULL,0,1000,NULL));
    } while(running);
    fclose(events);fclose(timings);fclose(mapping);curl_multi_cleanup(multi);curl_global_cleanup();
    if(done!=COUNT || failed || connects!=1) { fprintf(stderr,"Completed=%d failed=%d connections=%ld\n",done,failed,connects);return 1; }
    qsort(durations,COUNT,sizeof(double),compare);
    printf("{\"page_s\":%.9f,\"request_median_s\":%.9f,\"request_p95_s\":%.9f,\"connections\":%ld,\"requests\":%d}\n",last-first,(durations[49]+durations[50])/2,durations[94],connects,done);
    return 0;
}
