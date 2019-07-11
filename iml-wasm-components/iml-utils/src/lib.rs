// Copyright (c) 2019 DDN. All rights reserved.
// Use of this source code is governed by a MIT-style
// license that can be found in the LICENSE file.

use iml_wire_types::LockChange;
use regex::Regex;
use std::{
    cmp,
    collections::{HashMap, HashSet},
    mem,
};

pub fn extract_api(s: &str) -> Option<&str> {
    let re = Regex::new(r"^/?api/[^/]+/(\d+)/?$").unwrap();

    let x = re.captures(s)?;

    x.get(1).map(|x| x.as_str())
}

pub fn format_bytes(bytes: f64, precision: Option<usize>) -> String {
    let units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB", "ZiB", "YiB"];

    let bytes = bytes.max(0.0);
    let pwr = (bytes.ln() / 1024_f64.ln()).floor() as i32;
    let pwr = cmp::min(pwr, (units.len() - 1) as i32);
    let pwr = cmp::max(pwr, 0);
    let bytes = bytes / 1024_f64.powi(pwr);

    let bytes = format!("{:.*}", precision.unwrap_or(1), bytes);

    format!("{} {}", bytes, units[pwr as usize])
}

#[derive(Debug, Copy, Clone)]
pub enum WatchState {
    Watching,
    Open,
    Close,
}

impl Default for WatchState {
    fn default() -> Self {
        WatchState::Close
    }
}

impl WatchState {
    pub fn is_open(self) -> bool {
        match self {
            WatchState::Open => true,
            _ => false,
        }
    }
    pub fn is_watching(self) -> bool {
        match self {
            WatchState::Watching => true,
            _ => false,
        }
    }
    pub fn should_update(self) -> bool {
        self.is_watching() || self.is_open()
    }
    pub fn update(&mut self) {
        match self {
            WatchState::Close => {
                mem::replace(self, WatchState::Watching);
            }
            WatchState::Watching => {
                mem::replace(self, WatchState::Open);
            }
            WatchState::Open => {
                mem::replace(self, WatchState::Close);
            }
        }
    }
}

/// A map of locks in which the key is a composite id string in the form `composite_id:id`
pub type Locks = HashMap<String, HashSet<LockChange>>;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_extract_api_success() {
        assert_eq!(extract_api("/api/host/10").unwrap(), "10");
        assert_eq!(extract_api("/api/host/10/").unwrap(), "10");
        assert_eq!(extract_api("api/host/10").unwrap(), "10");
    }

    #[test]
    fn test_extract_api_failure() {
        assert_eq!(extract_api("foo"), None);
    }

    #[test]
    fn test_format_bytes_success() {
        assert_eq!(format_bytes(320.0, Some(0)), "320 B");
        assert_eq!(format_bytes(200_000.0, Some(1)), "195.3 KiB");
        assert_eq!(format_bytes(3_124_352.0, Some(3)), "2.980 MiB");
        assert_eq!(format_bytes(432_303_020_202.0, Some(3)), "402.614 GiB");
        assert_eq!(format_bytes(5_323_330_102_372.0, Some(2)), "4.84 TiB");
        assert_eq!(format_bytes(1000.0, Some(0)), "1000 B");
        assert_eq!(format_bytes(1024.0, Some(3)), "1.000 KiB");
        assert_eq!(format_bytes(4326.0, Some(4)), "4.2246 KiB");
        assert_eq!(format_bytes(3_045_827_469.0, Some(3)), "2.837 GiB");
        assert_eq!(format_bytes(84_567_942_345_572_238.0, Some(2)), "75.11 PiB");
        assert_eq!(
            format_bytes(5_213_456_204_567_832_146_028.0, Some(3)),
            "4.416 ZiB"
        );
        assert_eq!(format_bytes(139_083_776.0, Some(1)), "132.6 MiB");
    }
}