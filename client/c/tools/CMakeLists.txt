# CLI tools.

add_executable(shesha-iq shesha_iq.c)
target_link_libraries(shesha-iq PRIVATE shesha_client)
target_compile_options(shesha-iq PRIVATE -Wall -Wextra)
