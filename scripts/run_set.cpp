#include <algorithm>
#include <chrono>
#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <iostream>
#include <map>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <unordered_set>
#include <utility>
#include <vector>

#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

extern "C" {
#include "../rpm-build/lib/set.h"
}

namespace {

using Clock = std::chrono::steady_clock;
using Nanoseconds = std::chrono::nanoseconds;
using LabelSet = std::set<std::string>;

struct CommandResult {
    int exit_code;
    std::string output;
};

struct Timings {
    std::int64_t set_new_ns = 0;
    std::int64_t set_add_total_ns = 0;
    std::int64_t set_fini_ns = 0;
    std::int64_t set_free_ns = 0;

    std::int64_t total_ns() const
    {
        return set_new_ns + set_add_total_ns + set_fini_ns + set_free_ns;
    }
};

struct SetResult {
    std::string value;
    Timings timings;
};

struct Options {
    std::filesystem::path input;
    std::optional<int> bpp;
};

std::int64_t elapsed_ns(Clock::time_point start, Clock::time_point finish)
{
    return std::chrono::duration_cast<Nanoseconds>(finish - start).count();
}

std::string shortened(const std::string& value, std::size_t limit = 1200)
{
    if (value.size() <= limit) {
        return value;
    }
    return value.substr(0, limit) + "\n... output truncated ...";
}

CommandResult run_command(const std::vector<std::string>& command,
                          const std::map<std::string, std::string>& environment = {})
{
    if (command.empty()) {
        throw std::runtime_error("empty command");
    }

    int output_pipe[2];
    if (pipe(output_pipe) != 0) {
        throw std::runtime_error("pipe failed: " + std::string(std::strerror(errno)));
    }

    const pid_t child = fork();
    if (child < 0) {
        const int saved_errno = errno;
        close(output_pipe[0]);
        close(output_pipe[1]);
        throw std::runtime_error("fork failed: " + std::string(std::strerror(saved_errno)));
    }

    if (child == 0) {
        close(output_pipe[0]);
        if (dup2(output_pipe[1], STDOUT_FILENO) < 0 || dup2(output_pipe[1], STDERR_FILENO) < 0) {
            _exit(126);
        }
        close(output_pipe[1]);

        setenv("LC_ALL", "C", 1);
        for (const auto& [name, value] : environment) {
            setenv(name.c_str(), value.c_str(), 1);
        }

        std::vector<char*> argv;
        argv.reserve(command.size() + 1);
        for (const std::string& argument : command) {
            argv.push_back(const_cast<char*>(argument.c_str()));
        }
        argv.push_back(nullptr);
        execvp(argv[0], argv.data());
        _exit(127);
    }

    close(output_pipe[1]);
    std::string output;
    char buffer[16384];
    while (true) {
        const ssize_t count = read(output_pipe[0], buffer, sizeof(buffer));
        if (count > 0) {
            output.append(buffer, static_cast<std::size_t>(count));
            continue;
        }
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count < 0) {
            const int saved_errno = errno;
            close(output_pipe[0]);
            waitpid(child, nullptr, 0);
            throw std::runtime_error("read from child failed: " + std::string(std::strerror(saved_errno)));
        }
        break;
    }
    close(output_pipe[0]);

    int status = 0;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) {
            throw std::runtime_error("waitpid failed: " + std::string(std::strerror(errno)));
        }
    }

    int exit_code = 128;
    if (WIFEXITED(status)) {
        exit_code = WEXITSTATUS(status);
    } else if (WIFSIGNALED(status)) {
        exit_code += WTERMSIG(status);
    }
    return {exit_code, std::move(output)};
}

CommandResult checked_command(const std::vector<std::string>& command,
                              const std::map<std::string, std::string>& environment = {})
{
    CommandResult result = run_command(command, environment);
    if (result.exit_code != 0) {
        std::ostringstream message;
        message << command.front() << " exited with " << result.exit_code;
        if (!result.output.empty()) {
            message << ":\n" << shortened(result.output);
        }
        throw std::runtime_error(message.str());
    }
    return result;
}

std::string strip_symbol_version(std::string symbol)
{
    const std::size_t at = symbol.find('@');
    if (at != std::string::npos) {
        symbol.resize(at);
    }
    return symbol;
}

bool allowed_symbol_type(const std::string& type)
{
    static const std::unordered_set<std::string> allowed = {
        "NOTYPE", "OBJECT", "FUNC", "COMMON", "TLS", "IFUNC", "GNU_IFUNC",
    };
    return allowed.count(type) != 0;
}

bool allowed_symbol_binding(const std::string& binding)
{
    static const std::unordered_set<std::string> allowed = {
        "GLOBAL", "WEAK", "UNIQUE", "GNU_UNIQUE",
    };
    return allowed.count(binding) != 0;
}

bool allowed_symbol_visibility(const std::string& visibility)
{
    return visibility == "DEFAULT" || visibility == "PROTECTED";
}

bool special_symbol(const std::string& symbol)
{
    static const std::unordered_set<std::string> ignored = {
        "__bss_start", "_edata", "_end", "_fini", "_init",
    };
    return ignored.count(symbol) != 0;
}

LabelSet provided_labels(const std::filesystem::path& library)
{
    const CommandResult result = checked_command(
        {"eu-readelf", "--wide", "--dyn-syms", library.string()});
    LabelSet labels;
    std::istringstream output(result.output);
    std::string line;
    while (std::getline(output, line)) {
        std::istringstream fields(line);
        std::string number;
        std::string value;
        std::string size;
        std::string type;
        std::string binding;
        std::string visibility;
        std::string section;
        std::string symbol;
        if (!(fields >> number >> value >> size >> type >> binding >> visibility >> section >> symbol)) {
            continue;
        }
        if (number.empty() || number.back() != ':') {
            continue;
        }

        std::uint64_t symbol_value = 0;
        try {
            symbol_value = std::stoull(value, nullptr, 16);
        } catch (const std::exception&) {
            continue;
        }
        if (symbol_value == 0 && type != "TLS") {
            continue;
        }
        if (!allowed_symbol_type(type) || !allowed_symbol_binding(binding) ||
            !allowed_symbol_visibility(visibility)) {
            continue;
        }

        symbol = strip_symbol_version(std::move(symbol));
        if (symbol.empty() || special_symbol(symbol) ||
            symbol.find_first_of("()@") != std::string::npos) {
            continue;
        }
        labels.insert(std::move(symbol));
    }
    return labels;
}

LabelSet weak_undefined_labels(const std::filesystem::path& executable)
{
    const CommandResult result = checked_command({"nm", "--dynamic", executable.string()});
    LabelSet weak;
    std::istringstream output(result.output);
    std::string line;
    while (std::getline(output, line)) {
        std::istringstream fields(line);
        std::vector<std::string> parts;
        std::string part;
        while (fields >> part) {
            parts.push_back(part);
        }
        if (parts.size() != 2 || parts[0].size() != 1 ||
            std::string("wWvV").find(parts[0][0]) == std::string::npos) {
            continue;
        }
        weak.insert(strip_symbol_version(parts[1]));
    }
    return weak;
}

std::string program_interpreter(const std::filesystem::path& executable)
{
    const CommandResult result = checked_command(
        {"eu-readelf", "--program-headers", executable.string()});
    const std::string marker = "[Requesting program interpreter: ";
    const std::size_t start = result.output.find(marker);
    if (start == std::string::npos) {
        throw std::runtime_error("ELF executable has no PT_INTERP entry: " + executable.string());
    }
    const std::size_t value_start = start + marker.size();
    const std::size_t end = result.output.find(']', value_start);
    if (end == std::string::npos || end == value_start) {
        throw std::runtime_error("cannot parse PT_INTERP for " + executable.string());
    }
    return result.output.substr(value_start, end - value_start);
}

bool same_path(const std::string& lhs, const std::filesystem::path& rhs)
{
    std::error_code error;
    if (std::filesystem::equivalent(lhs, rhs, error)) {
        return true;
    }
    return std::filesystem::path(lhs).lexically_normal() == rhs.lexically_normal();
}

std::map<std::string, LabelSet> required_labels_by_provider(
    const std::filesystem::path& executable)
{
    const std::string interpreter = program_interpreter(executable);
    const std::map<std::string, std::string> environment = {
        {"LD_BIND_NOW", "1"},
        {"LD_DEBUG", "bindings"},
        {"LD_TRACE_LOADED_OBJECTS", "1"},
        {"LD_WARN", "1"},
    };
    const CommandResult result = checked_command(
        {interpreter, executable.string()}, environment);
    const LabelSet weak = weak_undefined_labels(executable);
    std::map<std::string, LabelSet> labels_by_provider;

    std::istringstream output(result.output);
    std::string line;
    while (std::getline(output, line)) {
        const std::string binding_marker = "binding file ";
        const std::size_t binding = line.find(binding_marker);
        if (binding == std::string::npos) {
            continue;
        }
        const std::size_t source_start = binding + binding_marker.size();
        const std::size_t source_end = line.find(" [", source_start);
        const std::size_t to = source_end == std::string::npos
                                   ? std::string::npos
                                   : line.find(" to ", source_end);
        const std::size_t provider_start = to == std::string::npos
                                               ? std::string::npos
                                               : to + std::string(" to ").size();
        const std::size_t provider_end = provider_start == std::string::npos
                                             ? std::string::npos
                                             : line.find(" [", provider_start);
        const std::size_t symbol_marker = provider_end == std::string::npos
                                              ? std::string::npos
                                              : line.find(" symbol ", provider_end);
        if (source_end == std::string::npos || to == std::string::npos ||
            provider_start == std::string::npos || provider_end == std::string::npos ||
            symbol_marker == std::string::npos) {
            continue;
        }

        const std::string source = line.substr(source_start, source_end - source_start);
        const std::string provider = line.substr(provider_start, provider_end - provider_start);
        if (!same_path(source, executable) || source == provider) {
            continue;
        }

        std::size_t symbol_start = symbol_marker + std::string(" symbol ").size();
        if (symbol_start < line.size() && (line[symbol_start] == '`' || line[symbol_start] == '\'')) {
            ++symbol_start;
        }
        std::size_t symbol_end = line.find('\'', symbol_start);
        if (symbol_end == std::string::npos) {
            symbol_end = line.find_first_of(" \t[", symbol_start);
        }
        if (symbol_end == std::string::npos) {
            symbol_end = line.size();
        }
        std::string symbol = strip_symbol_version(
            line.substr(symbol_start, symbol_end - symbol_start));
        if (symbol.empty() || weak.count(symbol) != 0) {
            continue;
        }
        labels_by_provider[provider].insert(std::move(symbol));
    }

    for (auto it = labels_by_provider.begin(); it != labels_by_provider.end();) {
        if (it->second.empty()) {
            it = labels_by_provider.erase(it);
        } else {
            ++it;
        }
    }
    return labels_by_provider;
}

int suggested_bpp(std::size_t provider_label_count)
{
    if (provider_label_count < 1) {
        provider_label_count = 1;
    }
    std::size_t value = provider_label_count - 1;
    int bits = 0;
    while (value != 0) {
        ++bits;
        value >>= 1;
    }
    return std::min(32, bits + 10);
}

SetResult build_set(const LabelSet& labels, int bpp)
{
    if (labels.empty()) {
        throw std::runtime_error("cannot build a set from zero labels");
    }

    Timings timings;
    auto start = Clock::now();
    struct set* value = set_new();
    auto finish = Clock::now();
    timings.set_new_ns = elapsed_ns(start, finish);
    if (value == nullptr) {
        throw std::runtime_error("set_new returned NULL");
    }

    start = Clock::now();
    for (const std::string& label : labels) {
        set_add(value, label.c_str());
    }
    finish = Clock::now();
    timings.set_add_total_ns = elapsed_ns(start, finish);

    start = Clock::now();
    const char* payload = set_fini(value, bpp);
    finish = Clock::now();
    timings.set_fini_ns = elapsed_ns(start, finish);
    if (payload == nullptr) {
        set_free(value);
        throw std::runtime_error("set_fini returned NULL");
    }
    std::string encoded = "set:" + std::string(payload);
    std::free(const_cast<char*>(payload));

    start = Clock::now();
    value = set_free(value);
    finish = Clock::now();
    timings.set_free_ns = elapsed_ns(start, finish);
    (void)value;
    return {std::move(encoded), timings};
}

enum class ElfKind {
    executable,
    shared_library,
};

ElfKind classify_elf(const std::filesystem::path& input)
{
    const CommandResult executable = run_command(
        {"eu-elfclassify", "--executable", input.string()});
    if (executable.exit_code == 0) {
        return ElfKind::executable;
    }
    if (executable.exit_code == 127) {
        throw std::runtime_error("eu-elfclassify is required but was not found");
    }

    const CommandResult shared = run_command(
        {"eu-elfclassify", "--shared", input.string()});
    if (shared.exit_code == 0) {
        return ElfKind::shared_library;
    }
    throw std::runtime_error("input is not a supported dynamic ELF executable or shared library: " +
                             input.string());
}

int parse_bpp(const std::string& value)
{
    std::size_t parsed = 0;
    int bpp = 0;
    try {
        bpp = std::stoi(value, &parsed);
    } catch (const std::exception&) {
        throw std::runtime_error("invalid bpp: " + value);
    }
    if (parsed != value.size() || bpp < 10 || bpp > 32) {
        throw std::runtime_error("bpp must be an integer in [10, 32]");
    }
    return bpp;
}

void usage(const char* program)
{
    std::cerr << "Usage: " << program << " [--bpp 10..32] ELF_PATH\n";
}

Options parse_options(int argc, char** argv)
{
    Options options;
    for (int index = 1; index < argc; ++index) {
        const std::string argument = argv[index];
        if (argument == "--help" || argument == "-h") {
            usage(argv[0]);
            std::exit(0);
        }
        if (argument == "--bpp") {
            if (++index >= argc) {
                throw std::runtime_error("--bpp requires a value");
            }
            options.bpp = parse_bpp(argv[index]);
            continue;
        }
        if (!argument.empty() && argument.front() == '-') {
            throw std::runtime_error("unknown option: " + argument);
        }
        if (!options.input.empty()) {
            throw std::runtime_error("exactly one ELF path is required");
        }
        options.input = argument;
    }
    if (options.input.empty()) {
        throw std::runtime_error("ELF path is required");
    }
    return options;
}

void print_header(const std::filesystem::path& input, ElfKind kind,
                  const std::optional<int>& bpp)
{
    std::cout << "input\t" << input.string() << '\n';
    std::cout << "kind\t"
              << (kind == ElfKind::executable ? "executable" : "shared-library") << '\n';
    std::cout << "bpp_mode\t" << (bpp.has_value() ? "override" : "auto") << '\n';
    std::cout << "role\tobject\tlabels\tbpp\tset\tset_new_ns\tset_add_total_ns\t"
                 "set_fini_ns\tset_free_ns\tset_api_total_ns\n";
}

void print_row(const std::string& role, const std::string& object,
               std::size_t label_count, int bpp, const SetResult& result)
{
    const Timings& timings = result.timings;
    std::cout << role << '\t' << object << '\t' << label_count << '\t' << bpp << '\t'
              << result.value << '\t' << timings.set_new_ns << '\t'
              << timings.set_add_total_ns << '\t' << timings.set_fini_ns << '\t'
              << timings.set_free_ns << '\t' << timings.total_ns() << '\n';
}

int run(const Options& options)
{
    std::error_code error;
    const std::filesystem::path input = std::filesystem::canonical(options.input, error);
    if (error || !std::filesystem::is_regular_file(input)) {
        throw std::runtime_error("input is not a readable regular file: " + options.input.string());
    }
    if (input.string().find_first_of("\t\r\n") != std::string::npos) {
        throw std::runtime_error("input path contains characters unsupported by TSV output");
    }

    const ElfKind kind = classify_elf(input);
    print_header(input, kind, options.bpp);

    if (kind == ElfKind::shared_library) {
        const LabelSet labels = provided_labels(input);
        if (labels.empty()) {
            throw std::runtime_error("no provided labels found in " + input.string());
        }
        const int bpp = options.bpp.value_or(suggested_bpp(labels.size()));
        print_row("provided", input.string(), labels.size(), bpp, build_set(labels, bpp));
        return 0;
    }

    const std::map<std::string, LabelSet> requirements = required_labels_by_provider(input);
    if (requirements.empty()) {
        throw std::runtime_error("no external dynamic symbol bindings found in " + input.string());
    }
    for (const auto& [provider, labels] : requirements) {
        std::size_t provider_count = 0;
        try {
            provider_count = provided_labels(provider).size();
        } catch (const std::exception& exception) {
            std::cerr << "warning: cannot inspect provider " << provider << ": "
                      << exception.what() << '\n';
        }
        const int bpp = options.bpp.value_or(
            suggested_bpp(provider_count == 0 ? labels.size() : provider_count));
        print_row("required", provider, labels.size(), bpp, build_set(labels, bpp));
    }
    return 0;
}

}  // namespace

int main(int argc, char** argv)
{
    try {
        return run(parse_options(argc, argv));
    } catch (const std::exception& exception) {
        std::cerr << "run_set: " << exception.what() << '\n';
        usage(argv[0]);
        return 2;
    }
}
