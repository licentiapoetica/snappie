/*
 * snappie-cast: ask KWin for a PipeWire stream of a window or a screen region,
 * or inject pointer/keyboard input.
 *
 * KWin only exposes zkde_screencast_unstable_v1 and org_kde_kwin_fake_input to
 * executables whose .desktop file lists them in X-KDE-Wayland-Interfaces
 * (that's how Spectacle does it), so this has to be a real binary, not a script.
 *
 *   snappie-cast window <uuid> <pointer>
 *   snappie-cast region <x> <y> <w> <h> <scale> <pointer>
 *
 * pointer: 1 hidden, 2 embedded.  Prints "node <id>", then keeps the stream
 * alive until stdin is closed.  Prints "closed" if KWin ends the stream
 * (window closed) and "error <msg>" on failure.
 *
 *   snappie-cast input
 *
 * Prints "ready", then reads commands from stdin, one per line, until EOF:
 *   move X Y        pointer to X, Y (global logical coordinates)
 *   scroll DY       vertical wheel, DY notches (fractions allowed, + = down,
 *                   one notch = 15 axis units like a real wheel)
 *   key CODE 0|1    release / press an evdev key code
 *   sync            answers "synced" once KWin has received everything before it
 * Bad commands print "error <msg>" and are otherwise ignored.
 */
#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <wayland-client.h>

#include "fake-input-client-protocol.h"
#include "zkde-screencast-unstable-v1-client-protocol.h"

static struct zkde_screencast_unstable_v1 *screencast;
static struct org_kde_kwin_fake_input *fake_input;
static int done, status;

static void global(void *data, struct wl_registry *reg, uint32_t name, const char *iface, uint32_t ver)
{
    if (strcmp(iface, zkde_screencast_unstable_v1_interface.name) == 0)
        /* v6 replaces "created" with "serial"; v5 still sends the node id */
        screencast = wl_registry_bind(reg, name, &zkde_screencast_unstable_v1_interface, ver < 5 ? ver : 5);
    else if (strcmp(iface, org_kde_kwin_fake_input_interface.name) == 0)
        /* v5 adds destroy; v6's keyboard_keysym isn't in our XML */
        fake_input = wl_registry_bind(reg, name, &org_kde_kwin_fake_input_interface, ver < 5 ? ver : 5);
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

/* One input command line.  Returns 0 to go on, -1 when the connection died. */
static int input_command(struct wl_display *dpy, char *line)
{
    uint32_t ver = org_kde_kwin_fake_input_get_version(fake_input);
    char cmd[16];
    double a, b;
    unsigned code, state;
    int n = 0;

    if (sscanf(line, "%15s%n", cmd, &n) != 1)
        return 0; /* blank line */
    const char *args = line + n;
    if (strcmp(cmd, "move") == 0 && sscanf(args, "%lf %lf", &a, &b) == 2) {
        if (ver < 3) {
            printf("error KWin's fake input is too old for absolute motion\n");
            return 0;
        }
        org_kde_kwin_fake_input_pointer_motion_absolute(fake_input, wl_fixed_from_double(a),
                                                        wl_fixed_from_double(b));
    } else if (strcmp(cmd, "scroll") == 0 && sscanf(args, "%lf", &a) == 1) {
        org_kde_kwin_fake_input_axis(fake_input, WL_POINTER_AXIS_VERTICAL_SCROLL, wl_fixed_from_double(a * 15));
    } else if (strcmp(cmd, "key") == 0 && sscanf(args, "%u %u", &code, &state) == 2) {
        if (ver < 4) {
            printf("error KWin's fake input is too old for keys\n");
            return 0;
        }
        org_kde_kwin_fake_input_keyboard_key(fake_input, code, state ? 1 : 0);
    } else if (strcmp(cmd, "sync") == 0) {
        if (wl_display_roundtrip(dpy) < 0)
            return -1;
        printf("synced\n");
        return 0;
    } else {
        printf("error bad command: %s\n", line);
        return 0;
    }
    return wl_display_flush(dpy) < 0 && errno != EAGAIN ? -1 : 0;
}

static int run_input(struct wl_display *dpy)
{
    if (!fake_input) {
        printf("error KWin did not offer org_kde_kwin_fake_input (helper not authorized - run helper/build.sh)\n");
        return 3;
    }
    org_kde_kwin_fake_input_authenticate(fake_input, "snappie", "scrolling capture");
    if (wl_display_roundtrip(dpy) < 0) {
        printf("error fake input was refused\n");
        return 1;
    }
    printf("ready\n");

    char buf[4096];
    size_t len = 0;
    struct pollfd fds[2] = {{wl_display_get_fd(dpy), POLLIN, 0}, {STDIN_FILENO, POLLIN, 0}};
    for (;;) {
        if (poll(fds, 2, -1) < 0 && errno != EINTR)
            break;
        if (fds[0].revents & (POLLERR | POLLHUP))
            break;
        if ((fds[0].revents & POLLIN) && wl_display_dispatch(dpy) < 0)
            break; /* KWin dropped us (protocol error) */
        if (fds[1].revents & (POLLIN | POLLHUP)) {
            ssize_t r = read(STDIN_FILENO, buf + len, sizeof buf - 1 - len);
            if (r <= 0)
                break; /* parent went away or closed stdin */
            len += r;
            buf[len] = 0;
            char *start = buf, *nl;
            while ((nl = strchr(start, '\n'))) {
                *nl = 0;
                if (input_command(dpy, start) < 0)
                    goto out;
                start = nl + 1;
            }
            len -= start - buf;
            memmove(buf, start, len);
            if (len == sizeof buf - 1)
                len = 0; /* absurdly long line: drop it */
        }
    }
out:
    if (org_kde_kwin_fake_input_get_version(fake_input) >= 5)
        org_kde_kwin_fake_input_destroy(fake_input);
    wl_display_roundtrip(dpy);
    return 0;
}

static int run_stream(struct wl_display *dpy, int argc, char **argv)
{
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
    return status;
}

int main(int argc, char **argv)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    int input = argc == 2 && strcmp(argv[1], "input") == 0;
    if (!input && !(argc == 4 && strcmp(argv[1], "window") == 0) && !(argc == 8 && strcmp(argv[1], "region") == 0)) {
        fprintf(stderr, "usage: %s window <uuid> <pointer> | region <x> <y> <w> <h> <scale> <pointer> | input\n",
                argv[0]);
        return 64;
    }
    struct wl_display *dpy = wl_display_connect(NULL);
    if (!dpy) {
        printf("error cannot connect to the Wayland display\n");
        return 2;
    }
    struct wl_registry *reg = wl_display_get_registry(dpy);
    wl_registry_add_listener(reg, &registry_listener, NULL);
    wl_display_roundtrip(dpy);

    int ret = input ? run_input(dpy) : run_stream(dpy, argc, argv);
    wl_display_disconnect(dpy);
    return ret;
}
