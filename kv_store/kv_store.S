.syntax unified
.cpu cortex-m4
.thumb

@ =============================================================================
@ Bucket layout (32 bytes, power-of-two for shift-based address calculation):
@   [0]:      status  (1 byte)
@   [1..4]:   key     (4 bytes)
@   [5..7]:   padding (3 bytes)
@   [8..23]:  val     (16 bytes)
@   [24..31]: padding (8 bytes, round to 32 for lsl #5)
@
@ Bucket N address = hash_table + (N << 5)  -- no MUL needed
@ Val field is at offset 8, always 8-byte aligned for LDM/STM safety.
@ =============================================================================

.equ BUCKET_SHIFT,   5          @ 1 << 5 = 32 bytes per bucket
.equ KEY_OFFSET,     1
.equ VAL_OFFSET,     8
.equ TABLE_SIZE,     256
.equ STATUS_EMPTY,   0
.equ STATUS_FILLED,  1
.equ STATUS_DELETED, 2

.section .bss
.align 5
.global hash_table
hash_table: .space (TABLE_SIZE << BUCKET_SHIFT)   @ 256 * 32 = 8192 bytes

.section .text

@ =============================================================================
@ fnv_hash(const char* key) -> r0 = hash & 0xFF
@   Hashes exactly 4 bytes. Keys are always 4 bytes wide per protocol.
@ =============================================================================
.global fnv_hash
fnv_hash:
    push {r4}
    ldr  r1, =0x811c9dc5        @ FNV offset basis
    ldr  r2, =0x01000193        @ FNV prime
    mov  r4, #0
hash_loop:
    ldrb r3, [r0, r4]
    eor  r1, r1, r3
    mul  r1, r1, r2
    add  r4, r4, #1
    cmp  r4, #4
    blt  hash_loop
    and  r0, r1, #0xFF
    pop  {r4}
    bx   lr

@ =============================================================================
@ kv_create(const char* key, const char* val) -> r0 = 1 (OK) / 0 (Full)
@ =============================================================================
.global kv_create
kv_create:
    push {r4-r10, lr}
    mov  r4, r0                 @ key ptr
    mov  r5, r1                 @ val ptr
    bl   fnv_hash
    mov  r6, r0                 @ current probe index
    ldr  r7, =hash_table
    mov  r9, #0                 @ first tombstone ptr (0 = none)
    mov  r10, #0                @ probe counter

create_probe:
    cmp  r10, #TABLE_SIZE
    bge  create_fail

    lsl  r8, r6, #BUCKET_SHIFT  @ r8 = index * 32 (shift, not mul)
    add  r8, r7, r8             @ r8 = bucket ptr

    ldrb r3, [r8]               @ status byte

    cmp  r3, #STATUS_EMPTY
    beq  use_empty

    cmp  r3, #STATUS_DELETED
    it   eq
    moveq r9, r8                @ record first tombstone
    beq  next_probe_create

    @ Filled: compare keys
    ldrb r0, [r4]
    ldrb r1, [r4, #1]
    ldrb r2, [r4, #2]
    ldrb r3, [r4, #3]
    orr  r0, r0, r1,  lsl #8
    orr  r0, r0, r2,  lsl #16
    orr  r0, r0, r3,  lsl #24
    
    ldrb r1, [r8, #KEY_OFFSET]
    ldrb r2, [r8, #KEY_OFFSET+1]
    ldrb r3, [r8, #KEY_OFFSET+2]
    ldrb lr, [r8, #KEY_OFFSET+3]
    orr  r1, r1, r2,  lsl #8
    orr  r1, r1, r3,  lsl #16
    orr  r1, r1, lr,  lsl #24
    cmp  r0, r1
    beq  insert_data            @ duplicate: overwrite value

next_probe_create:
    add  r6, r6, #1
    and  r6, r6, #0xFF
    add  r10, r10, #1
    b    create_probe

use_empty:
    cmp  r9, #0                 @ prefer tombstone slot if one was found
    it   ne
    movne r8, r9

insert_data:
    mov  r3, #STATUS_FILLED
    strb r3, [r8]

    ldrb r3, [r4]               @ write key byte-by-byte (safe unaligned)
    strb r3, [r8, #KEY_OFFSET]
    ldrb r3, [r4, #1]
    strb r3, [r8, #KEY_OFFSET+1]
    ldrb r3, [r4, #2]
    strb r3, [r8, #KEY_OFFSET+2]
    ldrb r3, [r4, #3]
    strb r3, [r8, #KEY_OFFSET+3]

    add  r8, r8, #VAL_OFFSET    @ val at offset 8, 8-byte aligned
    ldr  r0, [r5]               @ Unaligned-safe LDR on M4
    ldr  r1, [r5, #4]
    ldr  r2, [r5, #8]
    ldr  r3, [r5, #12]
    str  r0, [r8]
    str  r1, [r8, #4]
    str  r2, [r8, #8]
    str  r3, [r8, #12]

    mov  r0, #1
    pop  {r4-r10, pc}

create_fail:
    mov  r0, #0
    pop  {r4-r10, pc}

@ =============================================================================
@ kv_delete(const char* key) -> r0 = 1 (deleted) / 0 (not found)
@ =============================================================================
.global kv_delete
kv_delete:
    push {r4-r9, lr}
    mov  r4, r0
    bl   fnv_hash
    mov  r6, r0
    ldr  r7, =hash_table
    mov  r9, #0                 @ probe counter

del_probe:
    cmp  r9, #TABLE_SIZE
    bge  del_fail

    lsl  r8, r6, #BUCKET_SHIFT
    add  r8, r7, r8

    ldrb r3, [r8]

    cmp  r3, #STATUS_EMPTY
    beq  del_fail

    cmp  r3, #STATUS_FILLED
    bne  del_next

    ldrb r0, [r4]
    ldrb r1, [r4, #1]
    ldrb r2, [r4, #2]
    ldrb r3, [r4, #3]
    orr  r0, r0, r1,  lsl #8
    orr  r0, r0, r2,  lsl #16
    orr  r0, r0, r3,  lsl #24
    
    ldrb r1, [r8, #KEY_OFFSET]
    ldrb r2, [r8, #KEY_OFFSET+1]
    ldrb r3, [r8, #KEY_OFFSET+2]
    ldrb lr, [r8, #KEY_OFFSET+3]
    orr  r1, r1, r2,  lsl #8
    orr  r1, r1, r3,  lsl #16
    orr  r1, r1, lr,  lsl #24
    cmp  r0, r1
    beq  del_hit

del_next:
    add  r6, r6, #1
    and  r6, r6, #0xFF
    add  r9, r9, #1
    b    del_probe

del_hit:
    mov  r3, #STATUS_DELETED
    strb r3, [r8]
    mov  r0, #1
    pop  {r4-r9, pc}

del_fail:
    mov  r0, #0
    pop  {r4-r9, pc}

@ =============================================================================
@ kv_read(const char* key, char* out_val) -> r0 = 1 (found) / 0 (not found)
@ =============================================================================
.global kv_read
kv_read:
    push {r4-r9, lr}
    mov  r4, r0
    mov  r5, r1
    bl   fnv_hash
    mov  r6, r0
    ldr  r7, =hash_table
    mov  r9, #0                 @ probe counter

read_probe:
    cmp  r9, #TABLE_SIZE
    bge  read_fail

    lsl  r8, r6, #BUCKET_SHIFT
    add  r8, r7, r8

    ldrb r3, [r8]

    cmp  r3, #STATUS_EMPTY
    beq  read_fail

    cmp  r3, #STATUS_DELETED
    beq  read_next

    ldrb r0, [r4]
    ldrb r1, [r4, #1]
    ldrb r2, [r4, #2]
    ldrb r3, [r4, #3]
    orr  r0, r0, r1,  lsl #8
    orr  r0, r0, r2,  lsl #16
    orr  r0, r0, r3,  lsl #24
    
    ldrb r1, [r8, #KEY_OFFSET]
    ldrb r2, [r8, #KEY_OFFSET+1]
    ldrb r3, [r8, #KEY_OFFSET+2]
    ldrb lr, [r8, #KEY_OFFSET+3]
    orr  r1, r1, r2,  lsl #8
    orr  r1, r1, r3,  lsl #16
    orr  r1, r1, lr,  lsl #24
    cmp  r0, r1
    beq  read_hit

read_next:
    add  r6, r6, #1
    and  r6, r6, #0xFF
    add  r9, r9, #1
    b    read_probe

read_hit:
    add  r8, r8, #VAL_OFFSET    @ val at offset 8, always aligned
    ldr  r0, [r8]
    ldr  r1, [r8, #4]
    ldr  r2, [r8, #8]
    ldr  r3, [r8, #12]
    str  r0, [r5]
    str  r1, [r5, #4]
    str  r2, [r5, #8]
    str  r3, [r5, #12]
    mov  r0, #1
    pop  {r4-r9, pc}

read_fail:
    mov  r0, #0
    pop  {r4-r9, pc}
