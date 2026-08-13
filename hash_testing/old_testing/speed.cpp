/*
Compare speed of hash functions on fixed random byte strings.

Output: speed/{hash_name}.out, CSV, one row per input length.

Build example:
  cc -O3 -I. -c src/joaat/hash.c -o joaat.o
  cc -O3 -I. -c src/xxHash/xxhash.c -o xxhash.o
  g++ -O3 -std=c++17 -I. -Isrc/cityhash/src speed.cpp \
      src/cityhash/src/city.cc joaat.o xxhash.o -o speed_test

Run:
  ./speed_test [runs=7] [target_mib_per_run=32] [max_iters=200000]
*/

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <random>
#include <string>
#include <string_view>
#include <vector>

#include "src/cityhash/src/city.h"
#include "src/joaat/hash.h"
#include "src/xxHash/xxhash.h"

namespace fs = std::filesystem;

using Clock = std::chrono::steady_clock;
using HashFn = uint64_t (*)(const void *, size_t);

struct HashCase {
    std::string_view name;
    HashFn fn;
};

static uint64_t hash_xxh32(const void *data, size_t len)
{
    return XXH32(data, len, 0);
}

static uint64_t hash_xxh64(const void *data, size_t len)
{
    return XXH64(data, len, 0);
}

static uint64_t hash_xxh3_64(const void *data, size_t len)
{
    return XXH3_64bits(data, len);
}

static uint64_t hash_city32(const void *data, size_t len)
{
    return CityHash32(static_cast<const char *>(data), len);
}

static uint64_t hash_city64(const void *data, size_t len)
{
    return CityHash64(static_cast<const char *>(data), len);
}

static uint64_t hash_joaat(const void *data, size_t len)
{
    return joaat_hash_bytes(data, len);
}

static size_t iterations_for(size_t len, size_t target_bytes, size_t max_iters)
{
    if (len == 0) {
        return max_iters;
    }

    size_t iters = target_bytes / len;
    iters = std::max<size_t>(iters, 128);
    iters = std::min<size_t>(iters, max_iters);
    return iters;
}

static double median(std::vector<double> values)
{
    const size_t mid = values.size() / 2;
    std::nth_element(values.begin(), values.begin() + mid, values.end());
    if (values.size() % 2 != 0) {
        return values[mid];
    }

    const double hi = values[mid];
    std::nth_element(values.begin(), values.begin() + mid - 1, values.end());
    return (values[mid - 1] + hi) / 2.0;
}

static std::vector<unsigned char> random_bytes(size_t len)
{
    std::vector<unsigned char> data(len);
    std::mt19937_64 rng(0x415253565f686173ULL ^ len);
    for (unsigned char &byte : data) {
        byte = static_cast<unsigned char>(rng() & 0xffu);
    }
    return data;
}

static uint64_t bench_once(HashFn fn, const std::vector<unsigned char> &data, size_t iters)
{
    uint64_t sink = 0;
    const void *ptr = data.data();
    const size_t len = data.size();

    for (size_t i = 0; i < iters; ++i) {
        sink ^= fn(ptr, len) + 0x9e3779b97f4a7c15ULL + (sink << 6) + (sink >> 2);
    }

    return sink;
}

int main(int argc, char **argv)
{
    const int runs = argc > 1 ? std::max(1, std::atoi(argv[1])) : 7;
    const size_t target_mib = argc > 2 ? std::max(1, std::atoi(argv[2])) : 32;
    const size_t max_iters = argc > 3 ? std::max(1, std::atoi(argv[3])) : 200000;
    const size_t target_bytes = target_mib * 1024ULL * 1024ULL;

    const std::vector<size_t> lengths = {
        1, 4, 8, 16, 32, 64, 128, 256, 1024, 4096, 16384, 65536, 1048576,
    };

    const std::vector<HashCase> hashes = {
        {"xxh32", hash_xxh32},
        {"xxh64", hash_xxh64},
        {"xxh3_64", hash_xxh3_64},
        {"city32", hash_city32},
        {"city64", hash_city64},
        {"joaat", hash_joaat},
    };

    const fs::path out_dir = fs::path(__FILE__).stem();
    fs::create_directories(out_dir);

    volatile uint64_t global_sink = 0;

    for (const HashCase &hash : hashes) {
        const fs::path out_path = out_dir / (std::string(hash.name) + ".out");
        std::ofstream out(out_path);
        if (!out) {
            std::cerr << "failed to open " << out_path << '\n';
            return 1;
        }

        out << "hash,length,median_ns_per_hash,median_gib_per_s,iterations,runs,total_bytes_per_run,sink\n";
        out << std::fixed << std::setprecision(6);

        for (size_t len : lengths) {
            const std::vector<unsigned char> data = random_bytes(len);
            const size_t iters = iterations_for(len, target_bytes, max_iters);
            std::vector<double> ns_per_hash;
            std::vector<double> gib_per_s;
            uint64_t sink = 0;

            /* Warmup outside measured runs. */
            sink ^= bench_once(hash.fn, data, std::min<size_t>(iters, 1024));

            for (int run = 0; run < runs; ++run) {
                const auto start = Clock::now();
                sink ^= bench_once(hash.fn, data, iters);
                const auto stop = Clock::now();

                const double seconds = std::chrono::duration<double>(stop - start).count();
                const double bytes = static_cast<double>(len) * static_cast<double>(iters);
                ns_per_hash.push_back(seconds * 1e9 / static_cast<double>(iters));
                gib_per_s.push_back(bytes / seconds / (1024.0 * 1024.0 * 1024.0));
            }

            global_sink ^= sink;
            out << hash.name << ','
                << len << ','
                << median(ns_per_hash) << ','
                << median(gib_per_s) << ','
                << iters << ','
                << runs << ','
                << (len * iters) << ','
                << sink << '\n';
        }
    }

    std::cerr << "wrote " << out_dir << "/*.out; sink=" << global_sink << '\n';
    return 0;
}
