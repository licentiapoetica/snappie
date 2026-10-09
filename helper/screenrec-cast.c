/*
 * screenrec-cast: ask KWin for a PipeWire stream of a window or a screen region.
 *
 * KWin only exposes zkde_screencast_unstable_v1 to executables whose .desktop
 * file lists it in X-KDE-Wayland-Interfaces (that's how Spectacle does it), so
 * this has to be a real binary, not a script.
 *
 *   screenrec-cast window <uuid> <pointer>
 *   screenrec-cast region <x> <y> <w> <h> <scale> <pointer>
 *
 * pointer: 1 hidden, 2 embedded.  Prints "node <id>", then keeps the stream
 * alive until stdin is closed.  Prints "closed" if KWin ends the stream
 * (window closed) and "error <msg>" on failure.
 */
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <wayland-client.h>

#include "zkde-screencast-unstable-v1-client-protocol.h"

static struct zkde_screencast_unstable_v1 *screencast;
static int done, status;

static void global(void *data, struct wl_registry *reg, uint32_t name, const char *iface, uint32_t ver)
{
    if (strcmp(iface, zkde_screencast_unstable_v1_interface.name) == 0)
        /* v6 replaces "created" with "serial"; v5 still sends the node id */
        screencast = wl_registry_bind(reg, name, &zkde_screencast_unstable_v1_interface, ver < 5 ? ver : 5);
}

static void global_remove(void *data, struct wl_registry *reg, uint32_t name) {}

static const struct wl_registry_listener registry_listener = {global, global_remove};

static void on_closed(void *data, struct zkde_screencast_stream_unstable_v1 *s)
{
    printf("closed\n");
    done = 1;
}

static void on_created(void *data, struct zkde_screencast_stream_unstable_v1 *s, uint32_t node)
{
    printf("node %u\n", node);
}

static void on_failed(void *data, struct zkde_screencast_stream_unstable_v1 *s, const char *err)
{
    printf("error %s\n", err);
    done = 1;
    status = 1;
}

static void on_serial(void *data, struct zkde_screencast_stream_unstable_v1 *s, uint32_t hi, uint32_t lo) {}

static const struct zkde_screencast_stream_unstable_v1_listener stream_listener = {
    on_closed, on_created, on_failed, on_serial};

int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    struct wl_display *dpy = wl_display_connect(NULL);
    if (!dpy) {
        printf("error cannot connect to the Wayland display\n");
        return 2;
    }
    struct wl_registry *reg = wl_display_get_registry(dpy);
    wl_registry_add_listener(reg, &registry_listener, NULL);
    wl_display_roundtrip(dpy);
    if (!screencast) {
        printf("error KWin did not offer zkde_screencast (helper not authorized - run install.sh)\n");
        return 3;
    }

    struct zkde_screencast_stream_unstable_v1 *stream = NULL;
    if (argc == 4 && strcmp(argv[1], "window") == 0) {
        stream = zkde_screencast_unstable_v1_stream_window(screencast, argv[2], atoi(argv[3]));
    } else if (argc == 8 && strcmp(argv[1], "region") == 0) {
        if (zkde_screencast_unstable_v1_get_version(screencast) < 3) {
            printf("error KWin is too old for region streams\n");
            return 4;
        }
        stream = zkde_screencast_unstable_v1_stream_region(
            screencast, atoi(argv[2]), atoi(argv[3]), atoi(argv[4]), atoi(argv[5]),
            wl_fixed_from_double(atof(argv[6])), atoi(argv[7]));
    } else {
        fprintf(stderr, "usage: %s window <uuid> <pointer> | region <x> <y> <w> <h> <scale> <pointer>\n", argv[0]);
        return 64;
    }
    zkde_screencast_stream_unstable_v1_add_listener(stream, &stream_listener, NULL);
    wl_display_flush(dpy);

    struct pollfd fds[2] = {{wl_display_get_fd(dpy), POLLIN, 0}, {STDIN_FILENO, POLLIN, 0}};
    while (!done) {
        while (wl_display_prepare_read(dpy) != 0)
            wl_display_dispatch_pending(dpy);
        wl_display_flush(dpy);
        if (poll(fds, 2, -1) < 0) {
            wl_display_cancel_read(dpy);
            break;
        }
        if (fds[0].revents & POLLIN) {
            if (wl_display_read_events(dpy) < 0)
                break;
        } else {
            wl_display_cancel_read(dpy);
        }
        if (wl_display_dispatch_pending(dpy) < 0)
            break;
        if (fds[1].revents & (POLLIN | POLLHUP)) {
            char buf[64];
            if (read(STDIN_FILENO, buf, sizeof buf) <= 0)
                break; /* parent went away or closed stdin: stop */
        }
    }
    if (stream && !done)
        zkde_screencast_stream_unstable_v1_close(stream);
    zkde_screencast_unstable_v1_destroy(screencast);
    wl_display_roundtrip(dpy);
    wl_display_disconnect(dpy);
    return status;
}
