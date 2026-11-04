add_executable(test_codec test_codec.c)
target_link_libraries(test_codec PRIVATE shesha_client)
target_compile_options(test_codec PRIVATE -Wall -Wextra)

find_package(Threads REQUIRED)

add_executable(test_client test_client.c)
target_link_libraries(test_client PRIVATE shesha_client Threads::Threads)
target_compile_options(test_client PRIVATE -Wall -Wextra)

add_test(NAME test_codec COMMAND test_codec)
add_test(NAME test_client COMMAND test_client)
