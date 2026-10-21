// Compiles CanvasFingerprintManager.cpp and checks it against the Python port.
// Not part of the Firefox build.

#include "CanvasFingerprintManager.h"

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

int main(int argc, char** argv) {
  if (argc != 3) {
    std::fprintf(stderr, "usage: noise_selftest <seed> <rgba|bgra>\n");
    return 2;
  }
  uint32_t seed = static_cast<uint32_t>(std::strtoul(argv[1], nullptr, 10));
  std::string orderName(argv[2]);
  mozilla::dom::CanvasFingerprintManager::ByteOrder order =
      orderName == "bgra"
          ? mozilla::dom::CanvasFingerprintManager::ByteOrder::BGRA
          : mozilla::dom::CanvasFingerprintManager::ByteOrder::RGBA;

  std::vector<uint8_t> data;
  int byte = std::getchar();
  while (byte != EOF) {
    data.push_back(static_cast<uint8_t>(byte));
    byte = std::getchar();
  }
  mozilla::dom::CanvasFingerprintManager::ApplyNoise(data.data(), data.size(),
                                                      seed, order);
  if (!data.empty()) {
    std::fwrite(data.data(), 1, data.size(), stdout);
  }
  return 0;
}
