package com.example.sandbox

import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow

class GetUserUseCase(private val repository: UserRepository) {

    suspend operator fun invoke(id: String): User {
        require(id.isNotBlank()) { "User id must not be blank" }
        return repository.getUser(id)
    }

    fun observe(id: String): Flow<User> = flow {
        emit(invoke(id))
    }
}
