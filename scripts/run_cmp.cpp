#include <chrono>
#include <cstdint>
#include <iostream>
#include <string>

extern "C" {
#include "../rpm-build/lib/set.h"
}

namespace {

using Clock = std::chrono::steady_clock;
using Nanoseconds = std::chrono::nanoseconds;

void usage(const char* program)
{
    std::cerr << "Usage: " << program << " PROVIDER_SET REQUIRED_SET\n";
}

}  // namespace

int main(int argc, char** argv)
{
    if (argc != 3) {
        usage(argv[0]);
        return 2;
    }

    const std::string provider_set = argv[1];
    const std::string required_set = argv[2];

    const auto start = Clock::now();
    const int result = rpmsetcmp(provider_set.c_str(), required_set.c_str());
    const auto finish = Clock::now();
    const std::int64_t elapsed =
        std::chrono::duration_cast<Nanoseconds>(finish - start).count();

    std::cout << "provider_set\t" << provider_set << '\n';
    std::cout << "required_set\t" << required_set << '\n';
    std::cout << "cmp_result\t" << result << '\n';
    std::cout << "rpmsetcmp_ns\t" << elapsed << '\n';

    if (result == -3 || result == -4) {
        std::cerr << "run_cmp: rpmsetcmp rejected an input set (result=" << result << ")\n";
        return 1;
    }
    return 0;
}
