# Reverse Remote Play — PS4 app (OpenOrbis toolchain)
#
# Modeled on the OpenOrbis samples' Makefile. Requires clang + ld.lld and the
# OO_PS4_TOOLCHAIN environment variable (see install/ps4env.sh).
#
# Build:   source install/ps4env.sh && cd ps4 && make
# Output:  IV0000-BREW00300_00-REMOTEPLAYREV000.pkg
# Clean:   make clean

# ---- Package metadata (must be 12 chars for TITLE_ID, 36 for CONTENT_ID) ----
TITLE       := Reverse Remote Play
VERSION     := 1.00
TITLE_ID    := BREW00300
CONTENT_ID  := IV0000-BREW00300_00-REMOTEPLAYREV000

JPEGDIR     := third_party/libjpeg
FFMPEGDIR   := third_party/ffmpeg

# Libraries linked into the ELF:
#   libjpeg-turbo is a static archive built for the PS4 (install/build-libjpeg-ps4.sh).
#   libavcodec/libswscale/libavutil are a minimal FFmpeg H.264 decoder build
#   (install/build-ffmpeg-ps4.sh). Order matters: swscale needs avutil, and
#   avcodec needs avutil, so list dependents first.
LIBS        := -lc -lkernel -lc++ -lSceUserService -lSceVideoOut -lScePad -lSceSysmodule \
               $(JPEGDIR)/lib/libjpeg.a \
               $(FFMPEGDIR)/lib/libavcodec.a $(FFMPEGDIR)/lib/libswscale.a \
               $(FFMPEGDIR)/lib/libavutil.a -lm

# ---- Locations ----
TOOLCHAIN   := $(OO_PS4_TOOLCHAIN)
COMMONDIR   := $(TOOLCHAIN)/samples/_common
SRCDIR      := src
INCDIR      := include
INTDIR      := build

# ---- Sources ----
# NOTE: we ship a local copy of the sample's Scene2D helper (src/graphics.cpp,
# include/graphics.h) so we can add BlitRGB; we only need COMMONDIR for log.h.
CFILES      := $(wildcard $(SRCDIR)/*.c)
CPPFILES    := $(wildcard $(SRCDIR)/*.cpp)
OBJS        := $(patsubst $(SRCDIR)/%.c,   $(INTDIR)/%.c.o,   $(CFILES)) \
               $(patsubst $(SRCDIR)/%.cpp, $(INTDIR)/%.cpp.o, $(CPPFILES))

ASSETS      := $(wildcard assets/**/*)
LIBMODULES  := $(wildcard sce_module/*)

# ---- Compiler / linker flags (copied from OpenOrbis samples) ----
CFLAGS      := --target=x86_64-pc-freebsd12-elf -fPIC -funwind-tables -c \
               -isysroot $(TOOLCHAIN) -isystem $(TOOLCHAIN)/include \
               -I$(INCDIR) -I$(COMMONDIR) -I$(JPEGDIR)/include -I$(FFMPEGDIR)/include
CXXFLAGS    := $(CFLAGS) -isystem $(TOOLCHAIN)/include/c++/v1
LDFLAGS     := -m elf_x86_64 -pie --script $(TOOLCHAIN)/link.x --eh-frame-hdr \
               -L$(TOOLCHAIN)/lib $(LIBS) $(TOOLCHAIN)/lib/crt1.o

# ---- Platform detection ----
UNAME_S     := $(shell uname -s)
ifeq ($(UNAME_S),Linux)
    CC      := clang
    CCX     := clang++
    LD      := ld.lld
    CDIR    := linux
endif
ifeq ($(UNAME_S),Darwin)
    CC      := /usr/local/opt/llvm/bin/clang
    CCX     := /usr/local/opt/llvm/bin/clang++
    LD      := /usr/local/opt/llvm/bin/ld.lld
    CDIR    := macos
endif

.PHONY: all clean
all: $(CONTENT_ID).pkg

$(INTDIR):
	@mkdir -p $(INTDIR)

$(CONTENT_ID).pkg: pkg.gp4
	$(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core pkg_build $< .

pkg.gp4: eboot.bin sce_sys/about/right.sprx sce_sys/param.sfo sce_sys/icon0.png $(LIBMODULES) $(ASSETS)
	$(TOOLCHAIN)/bin/$(CDIR)/create-gp4 -out $@ --content-id=$(CONTENT_ID) --files "$^"

sce_sys/param.sfo: Makefile
	$(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_new $@
	$(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ APP_TYPE --type Integer --maxsize 4 --value 1
	$(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ APP_VER --type Utf8 --maxsize 8 --value '$(VERSION)'
	$(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ ATTRIBUTE --type Integer --maxsize 4 --value 0
	$(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ CATEGORY --type Utf8 --maxsize 4 --value 'gd'
	$(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ CONTENT_ID --type Utf8 --maxsize 48 --value '$(CONTENT_ID)'
	$(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ DOWNLOAD_DATA_SIZE --type Integer --maxsize 4 --value 0
	$(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ SYSTEM_VER --type Integer --maxsize 4 --value 0
	$(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ TITLE --type Utf8 --maxsize 128 --value '$(TITLE)'
	$(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ TITLE_ID --type Utf8 --maxsize 12 --value '$(TITLE_ID)'
	$(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ VERSION --type Utf8 --maxsize 8 --value '$(VERSION)'

eboot.bin: $(INTDIR) $(OBJS)
	$(LD) $(OBJS) -o $(INTDIR)/remote_play_rev.elf $(LDFLAGS)
	$(TOOLCHAIN)/bin/$(CDIR)/create-fself -in=$(INTDIR)/remote_play_rev.elf -out=$(INTDIR)/remote_play_rev.oelf --eboot "eboot.bin" --paid 0x3800000000000011

$(INTDIR)/%.c.o: $(SRCDIR)/%.c
	$(CC) $(CFLAGS) -o $@ $<

$(INTDIR)/%.cpp.o: $(SRCDIR)/%.cpp
	$(CCX) $(CXXFLAGS) -o $@ $<

$(INTDIR)/common_%.cpp.o: $(COMMONDIR)/%.cpp
	$(CCX) $(CXXFLAGS) -o $@ $<

clean:
	rm -f $(CONTENT_ID).pkg pkg.gp4 sce_sys/param.sfo eboot.bin $(OBJS)
	rm -f $(INTDIR)/remote_play_rev.elf $(INTDIR)/remote_play_rev.oelf
