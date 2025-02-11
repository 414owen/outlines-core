use criterion::{criterion_group, criterion_main, Criterion};
use std::hint::black_box;

fn fibonacci(n: u64) -> u64 {
    if n < 3 {
        n
    } else {
        fibonacci(n - 1) + fibonacci(n - 2) + fibonacci(n - 3)
    }
}

fn criterion_benchmark(c: &mut Criterion) {
    c.bench_function("fib 20", |b| b.iter(|| fibonacci(black_box(15))));
}

criterion_group!(benches, criterion_benchmark);
criterion_main!(benches);
