unsigned int hash(const char* str) {
  unsigned int hash = 0x9e3779b9;
  const unsigned char* p = (const unsigned char*)str;
  while (*p) {
    hash += *p++;
    hash += (hash << 10);
    hash ^= (hash >> 6);
  }
  hash += (hash << 3);
  hash ^= (hash >> 11);
  hash += (hash << 15);
  return hash;
}
