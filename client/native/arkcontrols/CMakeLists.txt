# Builds libarkcontrols.so: the arkImageInfo value type and ArkImageDataProvider for the
# official ark.controls QML module (arkcontrols.cpp), and the asynchronous image providers
# the Python renderers sit behind (imageprovider.cpp).
#
# Headers and moc come from a Qt SDK of the same version as the Qt PySide6 runs on
# (6.10.3, the version reMarkable ships; see requirements.txt); the library has no RPATH,
# so when Python loads it with ctypes it binds to the Qt libraries already loaded in the
# process.
#
#   cmake -S client/native/arkcontrols -B build/arkcontrols -DCMAKE_PREFIX_PATH=<Qt 6.10 SDK>/gcc_64
#   cmake --build build/arkcontrols
#
# install.py does this, with a Qt 6.10.3 SDK it finds or downloads (aqtinstall).
cmake_minimum_required(VERSION 3.21)
project(arkcontrols LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)
set(CMAKE_SKIP_RPATH ON)

find_package(Qt6 6.10 REQUIRED COMPONENTS Core Qml)

add_library(arkcontrols SHARED arkcontrols.cpp imageprovider.cpp)
target_link_libraries(arkcontrols PRIVATE Qt6::Core Qt6::Qml)
# QtGui and QtQuick are used through their headers and libraries only: their CMake
# packages would also require the OpenGL development files, which nothing here uses.
set(QT_LIB_DIR "${QT6_INSTALL_PREFIX}/${QT6_INSTALL_LIBS}")
foreach(module Gui Quick)
    find_library(QT_${module}_LIBRARY Qt6${module} PATHS "${QT_LIB_DIR}" NO_DEFAULT_PATH REQUIRED)
    target_link_libraries(arkcontrols PRIVATE "${QT_${module}_LIBRARY}")
endforeach()
set_target_properties(arkcontrols PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/../../src/ui/quick/native")
