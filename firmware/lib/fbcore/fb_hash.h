// Counter-based deterministic randomness.
//
// Every fault decision is a pure function of (seed, link, arrival index, stream).
// Nothing is consumed from a shared RNG state, so a decision never depends on
// timing, on how many other packets were seen, or on which other faults are
// enabled. The controller has a bit-exact Python port (faultbed/hashing.py) and
// uses it to audit the node's decisions after every run.
#pragma once
#include <stdint.h>

namespace fb {

enum Stream : uint8_t {
  STREAM_LOSS = 1,
  STREAM_GE = 2,        // Gilbert-Elliott state transition
  STREAM_CORRUPT = 3,
  STREAM_CORRUPT_BIT = 4,
  STREAM_JITTER = 5,
  STREAM_DUP = 6,
  STREAM_INTERVAL = 7,  // Poisson traffic inter-arrival
};

inline uint64_t splitmix64(uint64_t x) {
  x += 0x9E3779B97F4A7C15ULL;
  uint64_t z = x;
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
  return z ^ (z >> 31);
}

inline uint64_t streamKey(uint64_t seed, uint8_t a, uint8_t b, uint8_t stream) {
  return splitmix64(seed ^ ((uint64_t)a << 56) ^ ((uint64_t)b << 48) ^ ((uint64_t)stream << 40));
}

// 64 pseudo-random bits for event `index` on (a -> b, stream).
inline uint64_t draw(uint64_t seed, uint8_t a, uint8_t b, uint8_t stream, uint64_t index) {
  return splitmix64(streamKey(seed, a, b, stream) ^ index);
}

// Probability p in [0,1] -> 53-bit integer threshold. Compare (draw >> 11) < threshold.
// Integer comparison keeps C++ and Python bit-identical (no float rounding drift).
inline uint64_t probThreshold(double p) {
  if (p <= 0.0) return 0;
  if (p >= 1.0) return 1ULL << 53;
  return (uint64_t)(p * 9007199254740992.0);  // p * 2^53, exact scaling
}

inline bool bernoulli(uint64_t r, uint64_t threshold) { return (r >> 11) < threshold; }

}  // namespace fb
