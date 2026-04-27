#include <SDL2/SDL.h>
#include <rfb/rfbclient.h>
#include <fxlink/devices.h>
#include <fxlink/logging.h>
#include <stdio.h>
#include <signal.h>
#include <assert.h>
#include <stdbool.h>

/* Size of the calculator display */
#define CALC_WIDTH 396
#define CALC_HEIGHT 224
/* Number of monitors */
#define MONITOR_COUNT 2

#define min(a, b) ((a) < (b) ? (a) : (b))

/* Tracking data for a calculator-attached virtual monitor */
struct monitor {
    /* RFB/VNC client to get framebuffers from VNC server */
    rfbClient *client;
    /* SDL globals used to store the framebuffer and show it on-screen */
    SDL_Window *window;
    SDL_Surface *surface;
    /* Calculator */
    struct fxlink_device *calc;
    libusb_device *calc_unique_id;
    /* 16-bit framebuffer for the calculator (big endian) */
    uint16_t *fb16_be;
    /* Double buffer so we can convert next frame while sending current */
    uint16_t *fb16_be_tmp;
    /* Update flag for the calculator */
    bool needs_update;
    /* Whether the client has itself received any framebuffer yet */
    bool client_has_fb;
};

/* Application globals */
struct app {
    /* CLI options */
    bool display_calc;
    bool display_sdl;
    /* Calculator tracking */
    libusb_context *libusb_ctx;
    struct fxlink_device_list devices;
    /* All monitors */
    struct monitor *monitors[MONITOR_COUNT];
};
static struct app app = { 0 };

/* Cleanup all resources when execution finishes. */
static void cleanup(void)
{
    /* Wait for libusb to settle down */
    if(app.libusb_ctx) {
        while(fxlink_device_list_interrupt(&app.devices))
            libusb_handle_events(app.libusb_ctx);
    }

    for(int i = 0; i < MONITOR_COUNT; i++) {
        struct monitor *mon = app.monitors[i];
        if(!mon)
            continue;

        if(mon->client)
            rfbClientCleanup(mon->client);
        if(mon->window)
            SDL_DestroyWindow(mon->window);

        /* This device is managed by the device list */
        mon->calc = NULL;
        mon->calc_unique_id = NULL;

        free(mon->fb16_be);
        free(mon->fb16_be_tmp);
        free(mon);
    }

    SDL_Quit();
    fxlink_device_list_stop(&app.devices);
    if(app.libusb_ctx)
        libusb_exit(app.libusb_ctx);
}

/* Handle a framebuffer update. We immediately display to the SDL window but
   delay communication with the calculator since the calculator might be busy
   with a previous frame right now. */
static void fb_update(rfbClient *client)
{
    uint32_t *fb = (void *)client->frameBuffer;
    struct monitor *mon = rfbClientGetClientData(client, NULL);
    int min_w = min(client->width, CALC_WIDTH);
    int min_h = min(client->height, CALC_HEIGHT);

    if(app.display_sdl) {
        memset(mon->surface->pixels, 0, CALC_WIDTH * CALC_HEIGHT * 4);
        for(int y = 0; y < min_h; y++) {
            memcpy(mon->surface->pixels + y * mon->surface->pitch,
                fb + y * client->width, min_w * 4);
        }
        SDL_UpdateWindowSurface(mon->window);
    }

    mon->client_has_fb = true;
    mon->needs_update = true;
}

/* Convert a gint keycode to an rfbKeySym. This is just for show, the keymap is
   way too basic to be useful. */
static rfbKeySym keycode_to_rfbKeySym(int keycode)
{
    switch(keycode) {
    case 0x91 /* F1     */: return XK_F1;
    case 0x92 /* F2     */: return XK_F2;
    case 0x93 /* F3     */: return XK_F3;
    case 0x94 /* F4     */: return XK_F4;
    case 0x95 /* F5     */: return XK_F5;
    case 0x96 /* F6     */: return XK_F6;

    case 0x81 /* SHIFT  */: return XK_Shift_L;
    case 0x82 /* OPTN   */: return 0;
    case 0x83 /* VARS   */: return XK_Super_L;
    case 0x84 /* MENU   */: return 0;
    case 0x85 /* LEFT   */: return XK_Left;
    case 0x86 /* UP     */: return XK_Up;

    case 0x71 /* ALPHA  */: return XK_Control_L;
    case 0x72 /* SQUARE */: return 0;
    case 0x73 /* POWER  */: return 0;
    case 0x74 /* EXIT   */: return 0;
    case 0x75 /* DOWN   */: return XK_Down;
    case 0x76 /* RIGHT  */: return XK_Right;

    case 0x61 /* XOT    */: return 'a';
    case 0x62 /* LOG    */: return 'b';
    case 0x63 /* LN     */: return 'c';
    case 0x64 /* SIN    */: return 'd';
    case 0x65 /* COS    */: return 'e';
    case 0x66 /* TAN    */: return 'f';

    case 0x51 /* FRAC   */: return 'g';
    case 0x52 /* FD     */: return 'h';
    case 0x53 /* LEFTP  */: return 'i';
    case 0x54 /* RIGHTP */: return 'j';
    case 0x55 /* COMMA  */: return 'k';
    case 0x56 /* ARROW  */: return 'l';

    case 0x41 /* 7      */: return 'm';
    case 0x42 /* 8      */: return 'n';
    case 0x43 /* 9      */: return 'o';
    case 0x44 /* DEL    */: return XK_BackSpace;

    case 0x31 /* 4      */: return 'p';
    case 0x32 /* 5      */: return 'q';
    case 0x33 /* 6      */: return 'r';
    case 0x34 /* MUL    */: return 's';
    case 0x35 /* DIV    */: return 't';

    case 0x21 /* 1      */: return 'u';
    case 0x22 /* 2      */: return 'v';
    case 0x23 /* 3      */: return 'w';
    case 0x24 /* ADD    */: return 'x';
    case 0x25 /* SUB    */: return 'y';

    case 0x11 /* 0      */: return 'z';
    case 0x12 /* DOT    */: return ' ';
    case 0x13 /* EXP    */: return '"';
    case 0x14 /* NEG    */: return '-';
    case 0x15 /* EXE    */: return XK_Return;

    default: return 0;
    }
}

/* Handle incoming messages from the calculator. */
static void handle_calc_message(struct fxlink_message const *msg)
{
    /* Send messages to the first VNC server around */
    struct monitor *mon = NULL;
    for(int i = 0; i < MONITOR_COUNT && !mon; i++)
        mon = app.monitors[i];

    if(fxlink_message_is_apptype(msg, "cgvm", "pressed-keys")) {
        uint8_t *keys = msg->data;
        for(int i = 0; i < msg->size / 2; i++) {
            int code = keycode_to_rfbKeySym(keys[2*i]);
            int down = keys[2*i+1] ? TRUE : FALSE;
            if(code > 0 && mon)
                SendKeyEvent(mon->client, code, down);
        }
    }
    else {
        hlog("cgvm");
        log_("got unknown message: application '%.16s', type '%.16s'\n",
            msg->application, msg->type);
    }
}

static void usage(int rc)
{
    fprintf(stderr,
        "usage: cgvm_vnc [--calc] [--sdl]\n"
        "Connects to VNC server 127.0.0.1 and gets raw frames.\n"
        "--calc:  Send frames to a calculator (once one is detected).\n"
        "--sdl:   Show frames on an SDL window.\n");
    exit(rc);
}

static struct monitor *monitor_create(char *server)
{
    struct monitor *mon = calloc(1, sizeof *mon);
    assert(mon && "out of memory");

    rfbClient *client = rfbGetClient(8, 3, 4);
    if(!client) {
        fprintf(stderr, "rfbGetClient failed\n");
        return NULL;
    }
    rfbClientSetClientData(client, NULL, mon);

    client->FinishedFrameBufferUpdate = fb_update;
    client->width = CALC_WIDTH;
    client->height = CALC_HEIGHT;
    client->frameBuffer = malloc(CALC_WIDTH * CALC_HEIGHT * 4);
    assert(client->frameBuffer && "out of memory");

    /* Standard 32-bit xRGB */
    client->format.bitsPerPixel = 32;
    client->format.redShift = 16;
    client->format.greenShift = 8;
    client->format.blueShift = 0;
    client->format.redMax = 0xff;
    client->format.greenMax = 0xff;
    client->format.blueMax = 0xff;
    SetFormatAndEncodings(client);

    int argc = 4;
    char *argv[] = { "cgvm_vnc", "-encodings", "raw", server, NULL };

    if(!rfbInitClient(client, &argc, argv)) {
        fprintf(stderr, "rfbInitClient on server %s failed\n", server);
        return NULL;
    }
    mon->client = client;

    if(app.display_sdl) {
        if(!SDL_WasInit(SDL_INIT_VIDEO))
            SDL_Init(SDL_INIT_VIDEO);

        mon->window = SDL_CreateWindow("CG Virtual Monitor",
            SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED,
            CALC_WIDTH, CALC_HEIGHT, 0);
        mon->surface = SDL_GetWindowSurface(mon->window);

        assert(mon->surface->format->BytesPerPixel == 4);
        assert(mon->surface->format->format == SDL_PIXELFORMAT_RGB888);
    }

    mon->fb16_be = malloc(CALC_WIDTH * CALC_HEIGHT * 2);
    assert(mon->fb16_be && "out of memory");

    mon->fb16_be_tmp = malloc(CALC_WIDTH * CALC_HEIGHT * 2);
    assert(mon->fb16_be_tmp && "out of memory");

    return mon;
}

/* Static monitor assignment. If a calculator with the specified serial number
   is found, this function determines the only monitor that goes on it. */
static int monitor_assignment(struct fxlink_device *fdev)
{
    if(!fdev->calc || !fdev->calc->serial)
        return -1;

    if(!strcmp(fdev->calc->serial, "IGQcGRe9")) /* Lephe's Graph 90+E */
        return 0;
    if(!strcmp(fdev->calc->serial, "hULOJWGL")) /* Lephe's fx-CG 50 */
        return 1;

    return -1;
}

void monitor_update(struct monitor *mon, int mon_id)
{
    /* Check if the calculator disconnected */
    if(mon->calc) {
        bool still_here = false;
        for(int i = 0; i < app.devices.count; i++) {
            still_here |= app.devices.devices[i].dp == mon->calc_unique_id;
        }
        if(!still_here) {
            hlog("cgvm");
            log_("calculator disconnected!\n");
            mon->calc = NULL;
            mon->calc_unique_id = NULL;
        }
    }

    /* Check for devices ready to connect to */
    if(!mon->calc) {
        for(int i = 0; i < app.devices.count; i++) {
            struct fxlink_device *fdev = &app.devices.devices[i];
            char const *id = fxlink_device_id(fdev);

            if(!fxlink_device_ready_to_connect(fdev))
                continue;
            if(!fxlink_device_has_fxlink_interface(fdev)) {
                hlog("cgvm");
                log_("ignoring %s: no fxlink interface\n", id);
                continue;
            }

            int assigned_id = monitor_assignment(fdev);
            if(assigned_id >= 0 && assigned_id != mon_id) {
                hlog("cgvm");
                log_("reserving %s for its statically-assigned monitor %d\n",
                    id, assigned_id);
                continue;
            }

            if(!fxlink_device_claim_fxlink(fdev))
                continue;

            hlog("cgvm");
            log_("starting virtual monitor #%d on %s (serial: %s)\n",
                mon_id, id, fdev->calc->serial);
            mon->calc = fdev;
            mon->calc_unique_id = fdev->dp;
            fxlink_device_start_bulk_IN(fdev);
            mon->needs_update = true;
            break;
        }
    }

    /* Handle incoming transfers from the calc */
    if(mon->calc) {
        struct fxlink_message *msg = fxlink_device_finish_bulk_IN(mon->calc);
        if(msg) {
            handle_calc_message(msg);
            fxlink_message_free(msg, true);
            fxlink_device_start_bulk_IN(mon->calc);
        }
    }

    /* Send new frames */
    if(app.display_calc && mon->needs_update && mon->calc && !mon->calc->comm->ftransfer_OUT) {
        if(mon->client_has_fb) {
            uint32_t *fb = (void *)mon->client->frameBuffer;
            int min_w = min(mon->client->width, CALC_WIDTH);
            int min_h = min(mon->client->height, CALC_HEIGHT);

            memset(mon->fb16_be_tmp, 0, CALC_WIDTH * CALC_HEIGHT * 2);

            for(int y = 0; y < min_h; y++)
            for(int x = 0; x < min_w; x++) {
                uint32_t color = fb[y * mon->client->width + x];
                int R = (color >> 16) & 0xff;
                int G = (color >> 8) & 0xff;
                int B = (color & 0xff);

                /* Conversion to RGB565 */
                uint16_t c = ((R & 0xf8)<<8) | ((G & 0xfc)<<3) | ((B & 0xf8)>>3);
                mon->fb16_be_tmp[CALC_WIDTH * y + x] = (c >> 8) | (c << 8);
            }
        }
        else {
            memset(mon->fb16_be_tmp, 0x55, CALC_WIDTH * CALC_HEIGHT * 2);
        }

        if(fxlink_device_start_bulk_OUT(mon->calc, "cgvm", "fb",
                mon->fb16_be_tmp, CALC_WIDTH * CALC_HEIGHT * 2, false)) {
            mon->needs_update = false;
            uint16_t *tmp = mon->fb16_be_tmp;
            mon->fb16_be_tmp = mon->fb16_be;
            mon->fb16_be = tmp;
        }
    }
}

int main(int argc, char **argv)
{
    for(int i = 1; i < argc; i++) {
        if(!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help"))
            usage(0);
        else if(!strcmp(argv[i], "--calc"))
            app.display_calc = true;
        else if(!strcmp(argv[i], "--sdl"))
            app.display_sdl = true;
        else {
            fprintf(stderr, "error: unrecognized option '%s'\n", argv[i]);
            return 1;
        }
    }
    if(argc == 1)
        usage(0);
    if(!app.display_calc && !app.display_sdl)
        usage(1);

    atexit(cleanup);
    /* TODO: Sometimes when the calculator disconnects the wait on the RFB
       server loops and can't be killed by SIGINT or SIGTERM? */
    signal(SIGINT, exit);

    if(app.display_calc) {
        int rc;
        if((rc = libusb_init(&app.libusb_ctx)))
            return elog_libusb(rc, "error initializing libusb");

        libusb_set_option(app.libusb_ctx, LIBUSB_OPTION_LOG_LEVEL,
            LIBUSB_LOG_LEVEL_WARNING);
        fxlink_log_grab_libusb_logs();

        /* Track the list of connected calculators. */
        fxlink_device_list_track(&app.devices, app.libusb_ctx);
    }

    app.monitors[0] = monitor_create("127.0.0.1:5900");
    app.monitors[1] = monitor_create("127.0.0.1:5910");

    if(!app.monitors[0] && !app.monitors[1])
        return 1;

    while(1) {
        if(app.display_sdl) {
            SDL_Event e;
            while(SDL_PollEvent(&e)) {
                if(e.type == SDL_QUIT) {
                    fprintf(stderr, "SDL_QUIT: Exiting...\n");
                    exit(0);
                }
            }
        }

        for(int i = 0; i < MONITOR_COUNT; i++) {
            struct monitor *mon = app.monitors[i];
            if(!mon)
                continue;

            int rc = WaitForMessage(mon->client, 5000);
            if(rc < 0) {
                fprintf(stderr, "WaitForMessage() select: %d\n", rc);
                continue;
            }
            if(rc > 0 && !HandleRFBServerMessage(mon->client)) {
                fprintf(stderr, "HandleRFBServerMessage() failed\n");
                continue;
            }
        }

        /* Run libusb's event loop */
        if(app.libusb_ctx) {
            struct timeval zero_tv = { 0 };
            libusb_handle_events_timeout(app.libusb_ctx, &zero_tv);
            fxlink_device_list_refresh(&app.devices);
        }

        /* Update monitors so they can attach to calculators */
        for(int i = 0; i < MONITOR_COUNT; i++) {
            struct monitor *mon = app.monitors[i];
            if(mon)
                monitor_update(mon, i);
        }
    }

    return 0;
}
