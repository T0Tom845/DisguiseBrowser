/* This Source Code Form is subject to the terms of the Mozilla Public
 * License, v. 2.0. If a copy of the MPL was not distributed with this
 * file, You can obtain one at http://mozilla.org/MPL/2.0/. */

#ifndef mozilla_dom_CanvasFingerprintManager_h
#define mozilla_dom_CanvasFingerprintManager_h

#include <stddef.h>
#include <stdint.h>

namespace mozilla::dom {

// Sparse, stable noise on canvas readback. The algorithm is the one in
// disguisebrowser.camoufox.canvas_noise.apply_noise; the unit test compiles
// this file and checks the two stay identical.
class CanvasFingerprintManager final {
 public:
  enum class ByteOrder : uint8_t { RGBA, BGRA };

  // 0 means "off". The value is MaskConfig key "canvas:seed".
  static uint32_t Seed();

  // `aData` is a tightly packed width*height*4 copy that the caller is about
  // to hand to the page. The canvas backing store is not touched.
  static void ApplyNoise(uint8_t* aData, size_t aLength, uint32_t aSeed,
                          ByteOrder aOrder);
};

}  // namespace mozilla::dom

#endif  // mozilla_dom_CanvasFingerprintManager_h
