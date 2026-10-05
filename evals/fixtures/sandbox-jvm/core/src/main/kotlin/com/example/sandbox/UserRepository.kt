package com.example.sandbox

interface UserRepository {
    suspend fun getUser(id: String): User
}
