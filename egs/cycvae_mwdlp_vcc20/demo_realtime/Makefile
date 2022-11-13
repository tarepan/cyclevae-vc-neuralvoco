LDIR = lib
$(shell mkdir -p $(LDIR))
LIBNAME = mwdlp10cycvae
OUT = ${LDIR}/lib${LIBNAME}.a

CC = gcc
CFLAGS = -mavx2 -mfma -g -O3 -Wall -W -Wextra -fpic
LFLAGS = -lm

ODIR = obj
$(shell mkdir -p $(ODIR))

SDIR = src
IDIR = inc
BDIR = bin
$(shell mkdir -p $(BDIR))

INC = -I${IDIR}
LIB = -L${LDIR} -l${LIBNAME}
TARGET = test_cycvae_mwdlp

_OBJS = nnet.o mwdlp10net_cycvae.o kiss_fft.o freq.o wave.o nnet_data.o nnet_cv_data.o
OBJS = $(patsubst %,$(ODIR)/%,$(_OBJS))


# all: lib/libmwdlp10cycvae.a
#   gcc -mavx2 -mfma -g -O3
#      -Wall -W -Wextra -fpic
#      src/test_cycvae_mwdlp.c -Iinc -Llib
#      -lmwdlp10cycvae -lm -o bin/test_cycvae_mwdlp
# == gcc "Use x86 avx2" "Use x86 fma" "debug flag" "agressive Optim."
#      "full warning" "additional warning" "extra warning" "Position Independent Code"
#      "compile `src/test_cycvae_mwdlp.c`" "header from `inc`" "library from lib"
#      "link `mwdlp10cycvae`" "link `math.h`" "as `bin/test_cycvae_mwdlp`"
all: ${OUT}
	$(CC) $(CFLAGS) ${SDIR}/${TARGET}.c $(INC) ${LIB} ${LFLAGS} -o ${BDIR}/${TARGET}

$(OUT): $(OBJS) 
	ar rvs $(OUT) $^

# obj/%.o: src/%.c
# 	gcc -mavx2 -mfma -g -O3 -Wall -W -Wextra -fpic -Iinc -c -o $@ $< -lm
# 	gcc "AVX2/MFA, O3 optim, strong debug, fpic" "header from `inc`" "no link, obj" $@ $< "link `math.h`"
$(ODIR)/%.o: $(SDIR)/%.c
	$(CC) $(CFLAGS) $(INC) -c -o $@ $< ${LFLAGS}

.PHONY: clean

clean:
	rm -f $(ODIR)/*.o $(OUT) ${BDIR}/${TARGET}
