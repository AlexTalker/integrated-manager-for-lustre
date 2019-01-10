// Copyright (c) 2019 DDN. All rights reserved.
// Use of this source code is governed by a MIT-style
// license that can be found in the LICENSE file.

use std::env;

/// Get the environment variable or panic
fn get_var(name: &str) -> String {
    env::var(name).unwrap_or_else(|_| panic!("{} environment variable is required.", name))
}

/// Get the broker user from the env or panic
pub fn get_user() -> String {
    get_var("AMQP_BROKER_USER")
}

/// Get the broker password from the env or panic
pub fn get_password() -> String {
    get_var("AMQP_BROKER_PASSWORD")
}

/// Get the broker vhost from the env or panic
pub fn get_vhost() -> String {
    get_var("AMQP_BROKER_VHOST")
}

/// Get the broker host from the env or panic
pub fn get_host() -> String {
    localhost_to_ip(get_var("AMQP_BROKER_HOST"))
}

/// Get the broker port from the env or panic
pub fn get_port() -> String {
    get_var("AMQP_BROKER_PORT")
}

/// Get the server port from the env or panic
pub fn get_server_port() -> u16 {
    get_var("MESSAGING_PORT").parse().unwrap()
}

/// Get the server host from the env or panic
pub fn get_server_host() -> String {
    localhost_to_ip(get_var("PROXY_HOST"))
}

/// Translate the string localhost -> 127.0.0.1
fn localhost_to_ip(host: String) -> String {
    if host == "localhost" {
        "127.0.0.1".to_string()
    } else {
        host
    }
}

/// Get the AMQP server address or panic
pub fn get_addr() -> std::net::SocketAddr {
    format!("{}:{}", get_host(), get_port())
        .parse()
        .expect("Address not parsable to SocketAddr")
}
