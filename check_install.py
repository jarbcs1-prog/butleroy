import sys, ctypes, glob, os
print("python", sys.version.split()[0])
import llama_cpp
print("llama_cpp", llama_cpp.__version__)
d = os.path.dirname(llama_cpp.__file__)
libs = glob.glob(os.path.join(d, "lib", "*"))
for l in libs: print("  LIB", os.path.basename(l), "%.1f MB" % (os.path.getsize(l)/1048576))
print("--- backend info ---")
print(llama_cpp.llama_print_system_info().decode() if hasattr(llama_cpp,"llama_print_system_info") else "n/a")
