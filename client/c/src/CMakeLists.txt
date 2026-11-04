# shesha_client static library — dependency-free (POSIX sockets).

add_library(shesha_client STATIC
    client.c
    iq.c
)

target_include_directories(shesha_client
    PUBLIC
        $<BUILD_INTERFACE:${PROJECT_SOURCE_DIR}/include>
        $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>
    PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}
)

target_compile_options(shesha_client PRIVATE
    -Wall -Wextra -Wpedantic
)

# libm for the float codec (fabsf/lroundf).
target_link_libraries(shesha_client PUBLIC m)
