/*
Compare hash collision rates on random byte strings.

Output: collizions/{hash_name}.out, CSV, one row per input length and bit width.

Build example:
  cc -O3 -I. -c src/joaat/hash.c -o joaat.o
  cc -O3 -I. -c src/xxHash/xxhash.c -o xxhash.o
  g++ -O3 -std=c++17 -I. -Isrc/cityhash/src collizions.cpp \
      src/cityhash/src/city.cc joaat.o xxhash.o -o collizions_test

Run:
  ./collizions_test [runs=5] [samples=50000]
*/

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <random>
#include <string>
#include <string_view>
#include <unordered_set>
#include <vector>

#include "src/cityhash/src/city.h"
#include "src/joaat/hash.h"
#include "src/xxHash/xxhash.h"

namespace fs = std::filesystem;

using HashFn = uint64_t (*)(const void *, size_t);

struct HashCase {
    std::string_view name;
    HashFn fn;
};

struct CollisionStats {
    size_t collisions;
    size_t unique;
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

static uint64_t bit_mask(unsigned bits)
{
    return bits >= 64 ? ~0ULL : ((1ULL << bits) - 1ULL);
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

static std::vector<unsigned char> random_bytes(size_t len, uint64_t seed)
{
    std::vector<unsigned char> data(len);
    std::mt19937_64 rng(seed);
    for (unsigned char &byte : data) {
        byte = static_cast<unsigned char>(rng() & 0xffu);
    }
    return data;
}

static std::vector<uint64_t> random_hashes(HashFn fn, size_t len, size_t samples, uint64_t seed)
{
    std::vector<uint64_t> hashes;
    hashes.reserve(samples);

    std::mt19937_64 seed_rng(seed);
    for (size_t i = 0; i < samples; ++i) {
        const std::vector<unsigned char> data = random_bytes(len, seed_rng());
        hashes.push_back(fn(data.data(), data.size()));
    }

    return hashes;
}

static CollisionStats count_collisions(const std::vector<uint64_t> &hashes, unsigned bits)
{
    std::unordered_set<uint64_t> seen;
    seen.reserve(hashes.size() * 2);

    const uint64_t mask = bit_mask(bits);
    for (uint64_t hash : hashes) {
        seen.insert(hash & mask);
    }

    return {hashes.size() - seen.size(), seen.size()};
}

int main(int argc, char **argv)
{
    const int runs = argc > 1 ? std::max(1, std::atoi(argv[1])) : 5;
    const size_t samples = argc > 2 ? std::max(1, std::atoi(argv[2])) : 50000;

    const std::vector<size_t> lengths = {1, 4, 8, 16, 32, 64, 128, 256};
    const std::vector<unsigned> bit_widths = {10, 12, 16, 20, 24, 32};
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

    for (const HashCase &hash : hashes) {
        const fs::path out_path = out_dir / (std::string(hash.name) + ".out");
        std::ofstream out(out_path);
        if (!out) {
            std::cerr << "failed to open " << out_path << '\n';
            return 1;
        }

        out << "hash,length,bits,samples,runs,median_collisions,median_collision_rate,median_unique\n";
        out << std::fixed << std::setprecision(8);

        for (size_t len : lengths) {
            std::vector<std::vector<uint64_t>> run_hashes;
            run_hashes.reserve(runs);

            for (int run = 0; run < runs; ++run) {
                const uint64_t seed = 0x415253565f636f6cULL ^ (len << 8) ^ run;
                run_hashes.push_back(random_hashes(hash.fn, len, samples, seed));
            }

            for (unsigned bits : bit_widths) {
                std::vector<double> collision_values;
                std::vector<double> unique_values;
                collision_values.reserve(runs);
                unique_values.reserve(runs);

                for (const auto &hashes_for_run : run_hashes) {
                    const CollisionStats stats = count_collisions(hashes_for_run, bits);
                    collision_values.push_back(static_cast<double>(stats.collisions));
                    unique_values.push_back(static_cast<double>(stats.unique));
                }

                const double median_collisions = median(collision_values);
                const double median_unique = median(unique_values);
                out << hash.name << ','
                    << len << ','
                    << bits << ','
                    << samples << ','
                    << runs << ','
                    << median_collisions << ','
                    << (median_collisions / static_cast<double>(samples)) << ','
                    << median_unique << '\n';
            }
        }
    }

    std::cerr << "wrote " << out_dir << "/*.out\n";
    return 0;
}
