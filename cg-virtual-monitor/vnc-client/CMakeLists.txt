cmake_minimum_required(VERSION 3.10)
project(cgvm_vnc C)

set(CMAKE_C_STANDARD 11)

find_package(PkgConfig REQUIRED)

pkg_check_modules(LIBUSB REQUIRED libusb-1.0)
pkg_check_modules(SDL2 REQUIRED sdl2)
pkg_check_modules(LIBVNC REQUIRED libvncclient)

add_library(LibFxlink::LibFxlink STATIC IMPORTED)
set_target_properties(LibFxlink::LibFxlink PROPERTIES
    IMPORTED_LOCATION "/usr/local/lib/libfxlink.a"
    INTERFACE_INCLUDE_DIRECTORIES "/usr/local/include"
)

add_executable(cgvm_vnc src/main.c)

target_link_libraries(cgvm_vnc
    LibFxlink::LibFxlink
    ${LIBUSB_LIBRARIES}
    ${SDL2_LIBRARIES}
    ${LIBVNC_LIBRARIES}
    m
)

target_include_directories(cgvm_vnc PRIVATE
    ${LIBUSB_INCLUDE_DIRS}
    ${SDL2_INCLUDE_DIRS}
    ${LIBVNC_INCLUDE_DIRS}
    "/usr/local/include"
)
