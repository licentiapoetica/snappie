# KDE Share menu plugin. Snappie builds it into ~/.local/lib/qt6/plugins on its own
# (snappie/share_plugin.py); distro packages can build and install it system-wide:
#   cmake -B build snappie/purpose -DCMAKE_INSTALL_PREFIX=/usr && cmake --build build && cmake --install build
cmake_minimum_required(VERSION 3.16)
project(snappie_purpose LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_AUTOMOC ON)

find_package(Qt6 REQUIRED COMPONENTS Core DBus)
find_package(KF6Purpose REQUIRED)
find_package(KF6CoreAddons REQUIRED)

add_library(snappie_purpose MODULE snappie_purpose.cpp)
set_target_properties(snappie_purpose PROPERTIES PREFIX "")
target_link_libraries(snappie_purpose PRIVATE Qt6::Core Qt6::DBus KF6::Purpose KF6::CoreAddons)

install(TARGETS snappie_purpose DESTINATION ${QT6_INSTALL_PLUGINS}/kf6/purpose)
