#[cfg(test)]
mod tests {
    #[test]
    fn compiler_runs_in_the_target_environment() {
        let output = std::process::Command::new("rustc")
            .arg("-V")
            .output()
            .expect("The compiler must be on PATH");
        assert!(output.status.success(), "The compiler must run");
        assert!(String::from_utf8(output.stdout)
            .expect("The version must be UTF-8")
            .starts_with("rustc "));
    }
}
