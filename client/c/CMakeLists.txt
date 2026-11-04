cmake_minimum_required(VERSION 3.20)
project(shesha_client
    VERSION 0.2.0
    LANGUAGES C
)

set(CMAKE_C_STANDARD 17)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

option(SHESHA_CLIENT_SANITIZERS "Enable ASan + UBSan" OFF)
if(SHESHA_CLIENT_SANITIZERS)
    add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer)
    add_link_options(-fsanitize=address,undefined)
endif()

include(GNUInstallDirs)

add_subdirectory(src)

# Namespaced alias so add_subdirectory() consumers can link the same name
# as find_package() consumers.
add_library(shesha_client::shesha_client ALIAS shesha_client)

option(SHESHA_CLIENT_BUILD_TESTS "Build test suite" ON)
if(SHESHA_CLIENT_BUILD_TESTS)
    enable_testing()
    add_subdirectory(test)
endif()

option(SHESHA_CLIENT_BUILD_TOOLS "Build CLI tools" ON)
if(SHESHA_CLIENT_BUILD_TOOLS)
    add_subdirectory(tools)
endif()

# ─── Install + package export ────────────────────────────────────────────────

install(TARGETS shesha_client
    EXPORT shesha_clientTargets
    ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
    LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
)
install(DIRECTORY include/shesha
    DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}
)
install(EXPORT shesha_clientTargets
    FILE shesha_clientTargets.cmake
    NAMESPACE shesha_client::
    DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/shesha_client
)

include(CMakePackageConfigHelpers)

configure_package_config_file(
    cmake/shesha_clientConfig.cmake.in
    ${CMAKE_CURRENT_BINARY_DIR}/shesha_clientConfig.cmake
    INSTALL_DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/shesha_client
)

write_basic_package_version_file(
    ${CMAKE_CURRENT_BINARY_DIR}/shesha_clientConfigVersion.cmake
    COMPATIBILITY SameMajorVersion
)

install(FILES
    ${CMAKE_CURRENT_BINARY_DIR}/shesha_clientConfig.cmake
    ${CMAKE_CURRENT_BINARY_DIR}/shesha_clientConfigVersion.cmake
    DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/shesha_client
)
