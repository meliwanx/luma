#ifndef BRAND_CONFIG_H
#define BRAND_CONFIG_H

// Open-source default. The Windows build writes a replacement from
// BRAND_DISPLAY_NAME (-D or the environment) into the CMake binary dir,
// which is searched before this file. ASCII only so the resource compiler
// and the C++ compiler can share it.
#define BRAND_DISPLAY_NAME_UTF8 "Luma"

#endif
