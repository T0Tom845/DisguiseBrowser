/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

#include "CanvasFingerprintManager.h"

#include <cstring>
#include <vector>

// The selftest compiles this translation unit without a Camoufox tree.
#ifndef DISGUISE_CANVAS_NOISE_SELFTEST
#  include "MaskConfig.hpp"
#endif

namespace mozilla::dom {

namespace {

// 2/512 of the pixels that are not a flat interior: about 0.39%.
constexpr uint32_t kFnvOffset = 2166136261u;
constexpr uint32_t kFnvPrime = 16777619u;
constexpr uint32_t kRateMask = 511u;
constexpr uint32_t kRateLimit = 2u;

uint32_t Mix(uint32_t aSeed, uint32_t aContent, uint32_t aIndex) {
  uint32_t hash = aSeed * 0x9E3779B1u ^ aContent * 0x85EBCA77u ^
                  aIndex * 0xC2B2AE3Du;
  hash ^= hash >> 16;
  hash *= 0x7FEB352Du;
  hash ^= hash >> 15;
  hash *= 0x846CA68Bu;
  hash ^= hash >> 16;
  return hash;
}

void LogicalChannels(CanvasFingerprintManager::ByteOrder aOrder, int aOut[4]) {
  if (aOrder == CanvasFingerprintManager::ByteOrder::BGRA) {
    aOut[0] = 2;
    aOut[1] = 1;
    aOut[2] = 0;
  } else {
    aOut[0] = 0;
    aOut[1] = 1;
    aOut[2] = 2;
  }
  aOut[3] = 3;
}

bool FlatInterior(const uint8_t* aData, size_t aPixels, size_t aIndex) {
  const uint8_t* pixel = aData + aIndex * 4;
  if (aIndex > 0 && aIndex + 1 < aPixels) {
    return memcmp(pixel, pixel - 4, 4) == 0 &&
           memcmp(pixel, pixel + 4, 4) == 0;
  }
  const uint8_t* neighbor = aIndex == 0 ? pixel + 4 : pixel - 4;
  return memcmp(pixel, neighbor, 4) == 0;
}

bool HasMidChannel(const uint8_t* aPixel, const int aChannels[4]) {
  for (int i = 0; i < 3; ++i) {
    uint8_t value = aPixel[aChannels[i]];
    if (value != 0 && value != 255) {
      return true;
    }
  }
  return false;
}

bool Eligible(const uint8_t* aData, size_t aPixels, size_t aIndex,
              const int aChannels[4]) {
  const uint8_t* pixel = aData + aIndex * 4;
  if (pixel[3] == 0 || FlatInterior(aData, aPixels, aIndex)) {
    return false;
  }
  return HasMidChannel(pixel, aChannels);
}

void Perturb(uint8_t* aPixel, const int aChannels[4], uint32_t aHash) {
  int start = static_cast<int>((aHash >> 8) % 3u);
  for (int step = 0; step < 3; ++step) {
    int logical = (start + step) % 3;
    uint8_t* channel = aPixel + aChannels[logical];
    if (*channel != 0 && *channel != 255) {
      *channel ^= 1;
      return;
    }
  }
}

}  // namespace

uint32_t CanvasFingerprintManager::Seed() {
#ifdef DISGUISE_CANVAS_NOISE_SELFTEST
  return 0;
#else
  if (const auto value = MaskConfig::GetUint32("canvas:seed")) {
    return *value;
  }
  return 0;
#endif
}

void CanvasFingerprintManager::ApplyNoise(uint8_t* aData, size_t aLength,
                                           uint32_t aSeed, ByteOrder aOrder) {
  if (!aData || aSeed == 0 || aLength < 16 || aLength % 4 != 0) {
    return;
  }
  const size_t pixels = aLength / 4;
  if (pixels > UINT32_MAX) {
    return;
  }

  int channels[4];
  LogicalChannels(aOrder, channels);

  uint32_t content = kFnvOffset;
  bool uniform = true;
  for (size_t i = 0; i < pixels; ++i) {
    const uint8_t* pixel = aData + i * 4;
    if (uniform && i > 0 && memcmp(aData, pixel, 4) != 0) {
      uniform = false;
    }
    for (int channel = 0; channel < 4; ++channel) {
      content ^= pixel[channels[channel]];
      content *= kFnvPrime;
    }
  }
  if (uniform) {
    return;
  }

  struct Pick {
    uint32_t index;
    uint32_t hash;
  };
  std::vector<Pick> picks;
  picks.reserve(16);
  uint32_t bestHash = 0;
  uint32_t bestIndex = 0;
  bool haveBest = false;

  for (size_t i = 0; i < pixels; ++i) {
    if (!Eligible(aData, pixels, i, channels)) {
      continue;
    }
    uint32_t hash = Mix(aSeed, content, static_cast<uint32_t>(i));
    if (!haveBest || hash < bestHash) {
      haveBest = true;
      bestHash = hash;
      bestIndex = static_cast<uint32_t>(i);
    }
    if ((hash & kRateMask) < kRateLimit) {
      picks.push_back(Pick{static_cast<uint32_t>(i), hash});
    }
  }
  // A non-flat picture with no rate hit still changes, or two profiles of a
  // short antialiased string could keep the same pixels.
  if (picks.empty() && haveBest) {
    picks.push_back(Pick{bestIndex, bestHash});
  }
  for (const Pick& pick : picks) {
    Perturb(aData + static_cast<size_t>(pick.index) * 4, channels, pick.hash);
  }
}

}  // namespace mozilla::dom
